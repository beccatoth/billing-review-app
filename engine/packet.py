"""Close packets.

A close starts from the PRIOR close's carried-forward state (the "baseline"): customer accounts,
product-account mapping and the contract documents already on file. Each month the
team then adds only that month's files: usage, Stripe, NetSuite,
the messages/approvals, and any NEW contract documents (e.g. amendments).

After a close, save_baseline() writes the carried-forward state for the next month.
"""
from __future__ import annotations

import glob
import io
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .extract_terms import TERMS_COLUMNS, extract_terms, pdf_text
from .messages import APPROVAL_COLUMNS, parse_messages
from .sources import EXPECTED_EXTRA, SOURCES, read_table
from .util import parse_ts

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
BASELINE_DIR = os.path.join(DATA, "baseline_sep_2026")
SAMPLE_DIR = os.path.join(DATA, "sample_oct_2026")
SAMPLE_PERIOD = (2026, 10)
SAMPLE_CUTOFF = "2026-11-11T18:00:00-07:00"
DEMO_DIR = os.path.join(DATA, "rerun_demo_after_corrections")
DEMO_CUTOFF = "2026-11-14T18:00:00-07:00"

# Master data carried forward from the prior close (a monthly upload of the same file replaces it).
CARRIED_KEYS = ["accounts", "mapping"]
BASELINE_SHEETS = {"contract_terms": None, "salesforce_accounts": "accounts",
                   "product_account_mapping": "mapping"}


# ------------------------------------------------------------------ tables
def load_folder(folder: str) -> Dict[str, pd.DataFrame]:
    tables = {}
    for s in SOURCES:
        path = os.path.join(folder, s.filename)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                tables[s.key] = read_table(fh.read(), s.filename)
    return tables


def normalise_terms(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in TERMS_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    return df[TERMS_COLUMNS].fillna("")


def apply_billing_load_dates(terms: pd.DataFrame, register: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
    """Record each document's billing-load date from a contract register export.
    Set once: a date already on file in the repository is never overwritten."""
    terms = terms.copy()
    dates = {str(r["document_id"]).strip(): str(r.get("billing_loaded_at", "")).strip()
             for r in register.fillna("").to_dict("records")}
    n = 0
    for i, d in terms["document_id"].items():
        if d in dates and dates[d] and not str(terms.at[i, "billing_loaded_at"]).strip():
            terms.at[i, "billing_loaded_at"] = dates[d]
            n += 1
    return terms, n


def add_contract_pdf(terms: pd.DataFrame, data: bytes, label: str = "Extracted from PDF – new this month") -> Tuple[pd.DataFrame, Optional[dict]]:
    """Extract terms from an executed PDF and append them (existing documents are never overwritten)."""
    ex = extract_terms(data)
    if not ex["document_id"]:
        return terms, None
    if ex["document_id"] in set(terms["document_id"]):
        return terms, dict(ex, _existing=True)
    ex["term_source"] = label
    return pd.concat([terms, pd.DataFrame([ex])[TERMS_COLUMNS]], ignore_index=True), ex


# ------------------------------------------------------------------ baseline (prior close)
def load_baseline(folder: str = BASELINE_DIR) -> dict:
    tables = {k: v for k, v in load_folder(folder).items() if k in CARRIED_KEYS}
    with open(os.path.join(folder, "contract_terms.csv"), "rb") as fh:
        terms = normalise_terms(read_table(fh.read(), "contract_terms.csv"))
    pdfs = {os.path.basename(f): open(f, "rb").read() for f in sorted(glob.glob(os.path.join(folder, "contracts", "*.pdf")))}
    label = open(os.path.join(folder, "LABEL.txt")).read().strip() if os.path.exists(os.path.join(folder, "LABEL.txt")) else "Prior close"
    snapshot = {}
    for part in ("customers", "invoices"):
        f = os.path.join(folder, f"snapshot_{part}.csv")
        if os.path.exists(f):
            snapshot[part] = pd.read_csv(f, dtype={"ns_id": str, "unit_cents": str, "price_id": str, "product": str}, keep_default_na=False)
    return dict(tables=tables, terms=terms, pdfs=pdfs, label=label, snapshot=snapshot)


def baseline_to_excel(tables: Dict[str, pd.DataFrame], terms: pd.DataFrame, label: str, snapshot: Optional[dict] = None) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame([{"carried_forward_from": label, "saved_at": datetime.now().isoformat(timespec="minutes")}]).to_excel(xw, sheet_name="about", index=False)
        terms.to_excel(xw, sheet_name="contract_terms", index=False)
        for sheet, key in BASELINE_SHEETS.items():
            if key and key in tables:
                tables[key].to_excel(xw, sheet_name=sheet, index=False)
        for part, df in (snapshot or {}).items():
            df.to_excel(xw, sheet_name=f"snapshot_{part}", index=False)
    return buf.getvalue()


def baseline_from_excel(data: bytes) -> dict:
    xl = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str, keep_default_na=False)
    tables = {key: xl[sheet].fillna("") for sheet, key in BASELINE_SHEETS.items() if key and sheet in xl}
    terms = normalise_terms(xl.get("contract_terms", pd.DataFrame()))
    terms["term_source"] = terms["term_source"].replace("", "Carried forward")
    label = xl["about"]["carried_forward_from"].iloc[0] if "about" in xl and len(xl["about"]) else "Prior close"
    snapshot = {}
    for part in ("customers", "invoices"):
        if f"snapshot_{part}" in xl:
            df = xl[f"snapshot_{part}"]
            for col in df.columns:
                if col not in ("customer", "ns_id", "period", "billing", "usage_calc", "platform_calc", "invoice", "issued",
                               "line", "unit_cents", "service_from", "product", "price_id"):
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
            snapshot[part] = df
    return dict(tables=tables, terms=terms, pdfs={}, label=label, snapshot=snapshot)


# ------------------------------------------------------------------ a month's packet
def prepare_month(folder: str, baseline: dict) -> dict:
    """Baseline + this month's files in `folder` (CSVs, messages PDF, new contract PDFs)."""
    tables = dict(baseline["tables"])
    tables.update(load_folder(folder))
    terms = baseline["terms"].copy()
    pdfs = dict(baseline["pdfs"])
    new_docs: List[str] = []
    for f in sorted(glob.glob(os.path.join(folder, "contracts", "*.pdf")) + glob.glob(os.path.join(folder, "*.pdf"))):
        name = os.path.basename(f)
        if name in pdfs or "followup" in name.lower() or name.upper().startswith("START_HERE"):
            continue
        data = open(f, "rb").read()
        terms, ex = add_contract_pdf(terms, data)
        if ex and not ex.get("_existing"):
            pdfs[name] = data
            new_docs.append(ex["document_id"])
    reg = os.path.join(folder, "contract_register.csv")
    if os.path.exists(reg):
        terms, _ = apply_billing_load_dates(terms, read_table(open(reg, "rb").read(), "contract_register.csv"))
    approvals = pd.DataFrame(columns=APPROVAL_COLUMNS)
    msg = os.path.join(folder, "billing_followups.pdf")
    if os.path.exists(msg):
        approvals = parse_messages(pdf_text(open(msg, "rb").read()), list(tables["accounts"]["AccountName"]))
    ec = os.path.join(folder, "expected_counts.csv")
    counts = read_expected_counts(open(ec, "rb").read()) if os.path.exists(ec) else default_expected_counts()
    return dict(tables=tables, terms=terms, pdfs=pdfs, approvals=approvals, new_docs=new_docs, expected_counts=counts)


def default_expected_counts() -> Dict[str, int]:
    d = {s.key: s.expected_rows_default for s in SOURCES if s.expected_rows_default is not None}
    d.update(EXPECTED_EXTRA)
    return d


def read_expected_counts(data) -> Dict[str, int]:
    """expected_counts.csv: columns file, expected_rows (file = source file name)."""
    df = read_table(data, "expected_counts.csv")
    by_file = {s.filename: s.key for s in SOURCES}
    out = {}
    for r in df.to_dict("records"):
        key = by_file.get(r["file"].strip(), r["file"].strip())
        out[key] = int(r["expected_rows"]) if str(r["expected_rows"]).strip() else None
    return out


def sample_cutoff() -> datetime:
    return parse_ts(SAMPLE_CUTOFF)
