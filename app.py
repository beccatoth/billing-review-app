"""RelayCloud Billing Review – Streamlit app.

Run locally:  streamlit run app.py
"""
from __future__ import annotations

import base64
import calendar
import io
import os
import re
from datetime import date, datetime, time
from decimal import Decimal

import pandas as pd
import pypdfium2 as pdfium
import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from engine.extract_terms import TERMS_COLUMNS, extract_terms, pdf_text
from engine.messages import APPROVAL_COLUMNS, parse_messages
from engine.packet import (apply_billing_load_dates, DEMO_CUTOFF, DEMO_DIR, SAMPLE_DIR, SAMPLE_PERIOD, add_contract_pdf, baseline_from_excel,
                           baseline_to_excel, default_expected_counts, load_baseline, load_folder, prepare_month,
                           read_expected_counts, sample_cutoff)
from engine.report import (assumptions_table, billing_docs_table, checks_table, control_definitions, controls_table,
                           customer_summary, findings_table, journals_table, rollforward_table, to_excel, totals)
from engine.review import ACCOUNTS, ERROR, FOLLOW_UP, NEEDS_INFO, VALID, recognition_rows, run_review
from engine.snapshot import build_snapshot
from engine.terms import build_contracts
from engine.sources import SOURCE_BY_KEY, SOURCES, check_columns, read_table
from engine.util import D, DENVER, ZERO, fmt_int, fmt_usd, long_date, parse_date, parse_ts

st.set_page_config(page_title="RelayCloud Billing Review", page_icon="🧾", layout="wide")


# Streamlit treats "$...$" as LaTeX math. Money appears everywhere in this app, so escape
# dollar signs in every text element once, here, instead of at each call site.
def _escape_dollars(body):
    return re.sub(r"(?<!\\)\$", r"\\$", body) if isinstance(body, str) else body


def _patch(name):
    orig = getattr(DeltaGenerator, name)

    def wrapped(self, body, *args, **kwargs):
        return orig(self, _escape_dollars(body), *args, **kwargs)
    setattr(DeltaGenerator, name, wrapped)
    setattr(st, name, getattr(st._main, name))


st.markdown("""<style>
.st-key-contract_pdfs [data-testid="stFileUploaderFile"], .st-key-contract_pdfs [data-testid="stFileUploaderPagination"],
.st-key-month_files [data-testid="stFileUploaderFile"], .st-key-month_files [data-testid="stFileUploaderPagination"] {display: none;}
[class*="st-key-doclink_"] button[kind="tertiary"] {padding: 0; min-height: 0; height: auto;}
[class*="st-key-doclink_"] button[kind="tertiary"] p {color: #1a5fb4; text-decoration: underline; font-weight: 600;}
[class*="st-key-doclink_"] [data-testid="stHorizontalBlock"] {gap: 0.4rem;}
[data-testid="stMainBlockContainer"] {padding-top: 2.5rem;}
[class*="st-key-vgrid_"] {border: 1px solid rgba(49,51,63,0.12); border-radius: 0.5rem; overflow: hidden; gap: 0 !important;}
[class*="st-key-vgrid_"] [data-testid="stHorizontalBlock"] {gap: 0 !important; align-items: stretch;}
[class*="st-key-vgrid_"] [data-testid="stColumn"] {padding: 0.45rem 0.6rem; border-left: 1px solid rgba(49,51,63,0.08);
    display: flex; flex-direction: column; justify-content: center;}
[class*="st-key-vgrid_"] [data-testid="stColumn"] > div {width: 100%;}
[class*="st-key-vgrid_"] [data-testid="stColumn"] [data-testid="stVerticalBlock"] {gap: 0; justify-content: center;}
[class*="st-key-vgrid_"] [data-testid="stHorizontalBlock"] [data-testid="stHorizontalBlock"] {align-items: center; gap: 0.4rem !important;}
[class*="st-key-vgrid_"] .stDownloadButton, [class*="st-key-vgrid_"] .stButton {line-height: 1.5;}
[class*="st-key-vgrid_"] [data-testid="stColumn"]:first-child {border-left: 0;}
[class*="st-key-vrow_"], [class*="st-key-vkey_"] {border-top: 1px solid rgba(49,51,63,0.1);}
[class*="st-key-vhd_"] {background: rgb(248, 249, 251);}
[class*="st-key-vhd_"] p {color: rgba(49,51,63,0.6); font-weight: 400 !important;}
[class*="st-key-vkey_"] {background: #fff8e1;}
[class*="st-key-vgrid_"] [data-testid="stElementContainer"] {margin: 0 !important;}
[class*="st-key-vgrid_"] [data-testid="stMarkdownContainer"] {margin-bottom: 0 !important;}
[class*="st-key-vgrid_"] p {font-size: 0.875rem; margin: 0; line-height: 1.5;}
[class*="st-key-vgrid_"] button[kind="tertiary"] {padding: 0; min-height: 0; height: auto;}
[class*="st-key-vrow_"] button[kind="tertiary"] p {color: #1a5fb4; text-decoration: underline; font-weight: 400;}
[class*="st-key-vkey_"] button[kind="tertiary"] p {color: #1a5fb4; text-decoration: underline; font-weight: 700;}
[class*="st-key-custlink_"] button[kind="tertiary"] {padding: 0; min-height: 0;}
[class*="st-key-custlink_"] button[kind="tertiary"] p {color: #1a5fb4; text-decoration: underline; font-weight: 400;}
.st-key-contract_grid {gap: 0 !important;}
.st-key-contract_grid > div:first-child {
    position: sticky; top: 3.75rem; z-index: 5; background: var(--background-color, #fff);}
.st-key-contract_grid [data-testid="stHorizontalBlock"] {border-bottom: 1px solid rgba(49,51,63,0.12); padding: 6px 0; gap: 0.5rem; align-items: center;}
.st-key-contract_grid [data-testid="stVerticalBlock"] {gap: 0;}
.st-key-contract_grid [data-testid="stColumn"] {display: flex; flex-direction: column; justify-content: center;}
.st-key-contract_grid [data-testid="stColumn"] > div {width: 100%;}
.st-key-contract_grid [data-testid="stMarkdownContainer"] {margin: 0 !important;}
.st-key-contract_grid .stButton, .st-key-contract_grid [data-testid="stElementContainer"] {margin: 0 !important;}
.st-key-contract_grid [data-testid="stHorizontalBlock"]:first-child {border-bottom: 2px solid rgba(49,51,63,0.35);}
.st-key-contract_grid p {font-size: 0.88rem; margin: 0;}
.st-key-contract_grid button[kind="tertiary"] {padding: 0; min-height: 0; height: auto; line-height: 1.4;}
.st-key-contract_grid button[kind="tertiary"] p {color: #1a5fb4; text-decoration: underline; font-weight: 600;}
.st-key-contract_grid button[kind="tertiary"]:hover p {color: #0b3d91;}
</style>""", unsafe_allow_html=True)

if not getattr(DeltaGenerator, "_dollars_escaped", False):  # patch once per server process, not per rerun
    for _n in ("markdown", "caption", "info", "warning", "success", "error", "expander", "subheader", "title"):
        _patch(_n)
    DeltaGenerator._dollars_escaped = True

ss = st.session_state
ss.setdefault("tables", {})
ss.setdefault("table_files", {})
ss.setdefault("pdfs", {})
ss.setdefault("terms", pd.DataFrame(columns=TERMS_COLUMNS))
ss.setdefault("approvals", pd.DataFrame(columns=["reference", "date", "customer_name", "type", "amount",
                                                 "service_month", "status", "effect", "summary"]))
ss.setdefault("expected_counts", {})  # optional: drop an expected_counts.csv from the data provider
ss.setdefault("year", SAMPLE_PERIOD[0])
ss.setdefault("month", SAMPLE_PERIOD[1])
ss.setdefault("cutoff", sample_cutoff())
ss.setdefault("result", None)
ss.setdefault("decisions", {})
ss.setdefault("reviewer", "")
ss.setdefault("changes", None)
ss.setdefault("processed", set())
ss.setdefault("packet_name", "")
ss.setdefault("upload_log", {})

MONEY = st.column_config.NumberColumn(format="dollar")

CLS_ICON = {ERROR: "🔴", NEEDS_INFO: "🟡", FOLLOW_UP: "🔵", VALID: "🟢"}


# ======================================================================= helpers
def money_cols(df: pd.DataFrame) -> dict:
    return {c: MONEY for c in df.columns if df[c].dtype.kind == "f" and c not in ("Rate",)}


def show_df(df: pd.DataFrame, height=None, **kw):
    cfg = money_cols(df)
    if "Rate" in df.columns:
        cfg["Rate"] = st.column_config.NumberColumn(format="$%.2f")
    if height is not None:
        kw["height"] = height
    st.dataframe(df, hide_index=True, width="stretch", column_config=cfg, **kw)


def apply_baseline(b):
    """Start the close from the prior close's carried-forward state."""
    ss.baseline = b
    ss.tables = dict(b["tables"])
    ss.table_files = {k: f"carried forward – {b['label']}" for k in b["tables"]}
    ss.terms = b["terms"].copy()
    ss.pdfs = dict(b["pdfs"])
    ss.new_docs = []
    ss.approvals = pd.DataFrame(columns=APPROVAL_COLUMNS)
    ss.processed = set()
    ss.upload_log = {}


def load_sample(folder=SAMPLE_DIR, cutoff=None, label="October 2026 close files"):
    m = prepare_month(folder, ss.baseline)
    ss.tables, ss.terms, ss.pdfs, ss.approvals = m["tables"], m["terms"], m["pdfs"], m["approvals"]
    ss.table_files = {k: (f"{label}" if k in load_folder(folder) else f"carried forward – {ss.baseline['label']}") for k in m["tables"]}
    ss.new_docs = m["new_docs"]
    ss.expected_counts = m["expected_counts"]
    ss.year, ss.month = SAMPLE_PERIOD
    ss.cutoff = cutoff or sample_cutoff()
    ss.packet_name = label


def add_pdf(name: str, data: bytes) -> str:
    ss.terms, ex = add_contract_pdf(ss.terms, data)
    ss.setdefault("pdf_status", {})[name] = (
        ("⚠️", "Not a contract", "") if ex is None else
        ("✅", ex["document_id"], f"{ex['customer_name']} · already on file") if ex.get("_existing") else
        ("✅", ex["document_id"], ex["customer_name"]))
    if ex is None:
        return f"{name} → reference document (not a contract) – not used in calculations"
    if ex.get("_existing"):
        return f"{name} → {ex['document_id']} is already on file (carried forward) – nothing to add"
    ss.pdfs[name] = data
    ss.new_docs = list(dict.fromkeys(ss.get("new_docs", []) + [ex["document_id"]]))
    return f"{name} → **new contract document {ex['document_id']}** ({ex['customer_name']}): terms extracted – now in *All contract terms*"


def route_upload(name: str, data: bytes) -> str:
    low = name.lower()
    stem = os.path.splitext(low)[0]
    if low.endswith(".pdf"):
        if "followup" in stem or "message" in stem:
            names = list(ss.tables["accounts"]["AccountName"]) if "accounts" in ss.tables else []
            ss.approvals = parse_messages(pdf_text(data), names)
            n = int((ss.approvals["status"] == "approved").sum())
            return f"{name} → approvals log: {len(ss.approvals)} messages read, {n} approval(s) found (listed on Assumptions & open items)"
        return add_pdf(name, data)
    if stem.startswith("close_baseline") and low.endswith(".xlsx"):
        apply_baseline(baseline_from_excel(data))
        return f"{name} → prior close starting point replaced ({ss.baseline['label']})"
    if stem.startswith("contract_register"):
        ss.terms, n = apply_billing_load_dates(ss.terms, read_table(data, name))
        return f"{name} → billing-load dates recorded for {n} document(s)"
    for s in SOURCES:
        if stem == os.path.splitext(s.filename)[0]:
            ss.tables[s.key] = read_table(data, name)
            ss.table_files[s.key] = name
            return f"{name} → {s.label}"
    if stem.startswith("expected_counts"):
        ss.expected_counts = read_expected_counts(data)
        return f"{name} → expected record counts"
    if stem.startswith("approvals"):
        ss.approvals = read_table(data, name)
        return f"{name} → approvals log"
    if stem.startswith("reviewer_decisions"):
        df = read_table(data, name)
        ss.decisions = {r["Finding"]: dict(decision=r["Decision"], reviewer=r["Reviewer"], note=r["Note"]) for r in df.to_dict("records")}
        return f"{name} → reviewer decisions"
    return f"⚠️ {name}: not recognised – rename it to one of the expected file names below."


def slot_status(key: str):
    if key not in ss.tables:
        return "⬜ Missing", ""
    probs = check_columns(key, ss.tables[key])
    if probs:
        return "❌ Failed check", probs[0]
    src = ss.table_files.get(key, "")
    if src.startswith("carried forward"):
        return "↻ Carried forward", f"{fmt_int(len(ss.tables[key]))} rows ({src}). Upload this month's export if it changed."
    return "✅ Loaded", f"{fmt_int(len(ss.tables[key]))} rows from {src}"


MASTER_KEYS = ("accounts", "mapping")   # loaded on Contracts & Terms
MONTHLY_SOURCES = [s_ for s_ in SOURCES if s_.key not in MASTER_KEYS]


def ready_problems():
    out = [s.filename for s in MONTHLY_SOURCES if slot_status(s.key)[0].startswith("⬜")]
    bad = [f"{s.filename} ({slot_status(s.key)[1]})" for s in SOURCES if slot_status(s.key)[0].startswith("❌")]
    msgs = ([f"Missing: {', '.join(out)}"] if out else []) + bad
    return msgs


def _fkey(f):
    return (f.customer, f.area, f.title)


def do_run():
    prev = ss.result
    with st.spinner("Running the review…"):
        ss.result = run_review(ss.tables, ss.terms, ss.approvals, ss.year, ss.month, ss.cutoff, ss.expected_counts)
    if prev is not None and prev.period == ss.result.period:
        old = {_fkey(f): f for f in prev.findings}
        new = {_fkey(f): f for f in ss.result.findings}
        ss.changes = dict(resolved=[old[k] for k in old if k not in new], new=[new[k] for k in new if k not in old],
                          still=[new[k] for k in new if k in old and new[k].classification in (ERROR, NEEDS_INFO, FOLLOW_UP)],
                          prev_label=ss.get("prev_packet", ""))
    else:
        ss.changes = None
    ss.prev_packet = ss.packet_name


def show_changes():
    ch = ss.changes
    if not ch:
        return
    with st.container(border=True):
        c = st.columns(3)
        c[0].metric("Resolved since last run", len(ch["resolved"]))
        c[1].metric("New", len(ch["new"]))
        c[2].metric("Still open", len(ch["still"]))
        if ch["resolved"]:
            st.markdown("  \n".join(f"✅ {f.customer} – {f.title}" for f in ch["resolved"]))
        if ch["new"]:
            st.markdown("  \n".join(f"🆕 {f.customer} – {f.title}" for f in ch["new"]))


def need_result() -> bool:
    if ss.result is None:
        st.info("No review yet.")
        st.page_link(PAGE_START, label="Go to File Upload →", icon="📥")
        return False
    if ss.result.blocking:
        st.error("Resolve before relying on results:\n\n- " + "\n- ".join(ss.result.blocking))
    return True


def decisions_df() -> pd.DataFrame:
    return pd.DataFrame([{"Finding": k, "Decision": v["decision"], "Reviewer": v["reviewer"], "Note": v["note"]}
                         for k, v in ss.decisions.items()])


def sidebar():
    with st.sidebar:
        if ss.result is None:
            st.caption(f"Starting from the {ss.baseline['label']}")
            return
        r = ss.result
        t = totals(r)
        st.caption(f"{r.period_label} · cutoff {r.cutoff.astimezone(DENVER):%b %d %I:%M %p} MT")
        st.metric("Errors", t["n_errors"])
        st.metric("Net revenue correction", fmt_usd(t["rev_net"], True))
        st.download_button("⬇️ Results (Excel)", to_excel(r, ss.terms, ss.approvals, decisions_df()),
                           file_name=f"billing_review_{r.period[0]}_{r.period[1]:02d}.xlsx", width="stretch", type="primary")
        st.download_button("⬇️ Next month's starting point", baseline_to_excel(ss.tables, ss.terms, f"{r.period_label} close", build_snapshot(r)),
                           file_name=f"close_baseline_{r.period[0]}_{r.period[1]:02d}.xlsx", width="stretch",
                           help="Customers, workspaces and all contract terms. Drop it on File Upload next month.")


# ======================================================================= pages
def page_start():
    st.title("File Upload")

    st.subheader("Period")
    c1, c2, c3, c4 = st.columns(4)
    months = list(calendar.month_name)[1:]
    ss.month = months.index(c1.selectbox("Service month", months, index=ss.month - 1)) + 1
    ss.year = int(c2.number_input("Year", min_value=2020, max_value=2100, value=int(ss.year), step=1))
    cd = c3.date_input("Close cutoff", value=ss.cutoff.astimezone(DENVER).date())
    ctime = c4.time_input("Cutoff time (MT)", value=ss.cutoff.astimezone(DENVER).time().replace(tzinfo=None))
    ss.cutoff = datetime.combine(cd, ctime).replace(tzinfo=DENVER)

    st.subheader("Files")
    ups = st.file_uploader("Drop this month's files (CSV, Excel, PDF)", accept_multiple_files=True, type=["csv", "xlsx", "pdf"],
                           key="month_files")
    if ups:
        # tables first (the messages PDF needs the customer list), then PDFs; each file is processed once
        new = [u for u in ups if (u.name, u.size) not in ss.processed]
        new.sort(key=lambda u: (not u.name.lower().startswith("close_baseline"), u.name.lower().startswith("contract_register"),
                                u.name.lower().endswith(".pdf")))
        for u in new:
            ss.upload_log[u.name] = route_upload(u.name, u.getvalue())
            ss.processed.add((u.name, u.size))
        if new:
            ss.packet_name = f"uploaded files ({len(ups)})"
            ss.run_attempted = False
        bad = [u.name for u in ups if ss.upload_log.get(u.name, "").startswith("⚠️")]
        if bad:
            st.toast(f"Not recognised: {', '.join(bad)}", icon="⚠️")

    rows = []
    for s in MONTHLY_SOURCES:
        status, msg = slot_status(s.key)
        rows.append({"": status.split(" ")[0], "File": s.filename, "System": s.group.split(". ", 1)[1],
                     "Rows": f"{len(ss.tables[s.key]):,}" if s.key in ss.tables else ""})
    rows.append({"": "✅" if len(ss.approvals) else "⬜", "File": "billing_followups.pdf", "System": "Approvals",
                 "Rows": str(len(ss.approvals)) if len(ss.approvals) else ""})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=38 + 35 * len(rows),
                 column_config={"": st.column_config.TextColumn(width=40)})

    if st.button("▶️ Run review", type="primary"):
        ss.run_attempted = True
        if not ready_problems():
            do_run()
    probs = ready_problems()
    if ss.get("run_attempted") and probs:
        with st.container(border=True):
            st.markdown("**Can't run yet**")
            for p_ in probs:
                st.markdown(f"⬜ {p_}")
    elif ss.result is not None:
        t = totals(ss.result)
        with st.container(border=True):
            c = st.columns(4)
            c[0].metric("Errors", t["n_errors"])
            c[1].metric("Needs information", t["n_needs_info"])
            c[2].metric("Follow-ups", t["n_follow"])
            c[3].metric("Valid explanations", t["n_valid"])
            show_changes()
            st.page_link(PAGE_FINDINGS, label="Go to Findings and Next Steps →", icon="🚩")
        render_controls(ss.result)


def _fmt_term(fld, v):
    if v in ("", None):
        return ""
    try:
        if fld in ("platform_annual_fee", "platform_invoice_amount", "catch_up_charge", "incremental_consideration"):
            return f"${float(v):,.2f}"
        if fld == "overage_rate_per_run":
            return f"${float(v):.2f}"
        if fld == "included_runs_per_month":
            return f"{int(float(v)):,}"
    except ValueError:
        pass
    return str(v)


@st.cache_data(show_spinner=False)
def pdf_pages(data: bytes) -> list:
    """Render each PDF page to a PNG image (read-only view of the executed document)."""
    doc = pdfium.PdfDocument(data)
    out = []
    for page in doc:
        buf = io.BytesIO()
        page.render(scale=2).to_pil().save(buf, format="PNG")
        out.append(buf.getvalue())
    return out


@st.dialog("Executed document", width="large")
def contract_dialog(doc_id):
    data = ss.pdfs.get(f"{doc_id}.pdf")
    if not data:
        st.info("PDF not on file.")
        return
    # embedded as data URIs so the pages never depend on Streamlit's media store (which is reset on rerun)
    st.html("".join(f'<img src="data:image/png;base64,{base64.b64encode(img).decode()}" '
                    f'style="width:100%;border:1px solid #e5e7eb;margin-bottom:12px"/>' for img in pdf_pages(data)))


def page_contracts():
    st.title("Contracts & Terms")
    tab_all, tab_load = st.tabs(["All contract terms", "Load new contracts"])
    with tab_load:
        ups = st.file_uploader("Load new contracts", type=["pdf", "csv", "xlsx"], accept_multiple_files=True,
                               key="contract_pdfs", label_visibility="collapsed")
        if ups:
            new = [u for u in ups if (u.name, u.size) not in ss.processed]
            new.sort(key=lambda u: (u.name.lower().startswith("contract_register"), u.name.lower().endswith(".pdf")))
            for u in new:
                if u.name.lower().endswith(".pdf"):
                    ss.upload_log[u.name] = add_pdf(u.name, u.getvalue())
                else:
                    msg = route_upload(u.name, u.getvalue())
                    ss.upload_log[u.name] = msg
                    if u.name.lower().startswith("contract_register"):
                        ss.setdefault("pdf_status", {})[u.name] = ("✅", "Contract register", msg.split("→ ")[-1])
                        ss.processed.add((u.name, u.size))
                        continue
                    key = next((s_.key for s_ in SOURCES if s_.key in MASTER_KEYS and
                                os.path.splitext(u.name.lower())[0] == os.path.splitext(s_.filename)[0]), None)
                    ok = key is not None and not msg.startswith("⚠️") and not check_columns(key, ss.tables[key])
                    ss.setdefault("pdf_status", {})[u.name] = (
                        ("✅", SOURCE_BY_KEY[key].label, f"{len(ss.tables[key]):,} rows") if ok else ("⚠️", u.name, "Not recognised"))
                ss.processed.add((u.name, u.size))
            if new:
                ok = sum(ss.pdf_status[u.name][0] == "✅" for u in new)
                st.toast(f"{ok} of {len(new)} file(s) loaded", icon="✅" if ok == len(new) else "⚠️")
            for u in ups:
                icon, doc, cust = ss.pdf_status.get(u.name, ("⚠️", "", ""))
                c1, c2, c3 = st.columns([0.4, 3, 5])
                c1.markdown(icon)
                c2.markdown(f"**{doc or u.name}**")
                c3.markdown(cust)
    with tab_all:
        terms = ss.terms
        nd = set(ss.get("new_docs", []))
        is_am = terms["document_type"] == "amendment"
        c = st.columns(3)
        c[0].metric("Total contracts", len(terms))
        c[1].metric("Order forms", int((~is_am).sum()))
        c[2].metric("Amendments", int(is_am.sum()))
        view = pd.DataFrame({
            "Document": terms["document_id"],
            "Contract ID": terms["contract_id"],
            "Customer": terms["customer_name"].str.replace(r",? Inc\.?$", "", regex=True),
            "Loaded to billing": [billing_status(d, t_, c_) for d, t_, c_ in
                                  zip(terms["document_id"], terms["document_type"], terms["customer_name"])],
            "Effective": terms["effective_from"],
            "Billing": terms["billing_frequency"],
            "Annual fee": [_fmt_term("platform_annual_fee", v) for v in terms["platform_annual_fee"]],
            "Invoice": [_fmt_term("platform_invoice_amount", v) for v in terms["platform_invoice_amount"]],
            "Runs / mo": [_fmt_term("included_runs_per_month", v) for v in terms["included_runs_per_month"]],
            "Rate": [_fmt_term("overage_rate_per_run", v) for v in terms["overage_rate_per_run"]],
        })
        view["_order"] = terms["document_type"].ne("order_form")
        view = view.sort_values(["Customer", "_order", "Effective"]).drop(columns="_order")
        # A plain grid (not a selectable table): the document ID is a link that opens the contract.
        widths = [1.3, 1.05, 1.75, 1.05, 0.95, 0.8, 1.0, 0.95, 0.8, 0.6]
        open_idx = None
        with st.container(key="contract_grid"):
            hdr = st.columns(widths)
            for col, name in zip(hdr, view.columns):
                col.markdown(f"**{name}**")
            for idx, r in view.iterrows():
                cols = st.columns(widths)
                if cols[0].button(r["Document"], key=f"open_{r['Document']}", type="tertiary"):
                    open_idx = idx
                for col, name in zip(cols[1:], view.columns[1:]):
                    col.markdown(r[name] or "–")
        if open_idx is not None:
            contract_dialog(view.loc[open_idx, "Document"])


def page_health():
    st.title("Data Quality")
    if not need_result():
        return
    r = ss.result
    df = checks_table(r)
    c = df["Result"].value_counts()
    a, b, d, e = st.columns(4)
    a.metric("Fail", int(c.get("Fail", 0)))
    b.metric("Warning", int(c.get("Warning", 0)))
    d.metric("Info", int(c.get("Info", 0)))
    e.metric("Pass", int(c.get("Pass", 0)))
    icon = {"Pass": "✅", "Fail": "❌", "Warning": "⚠️", "Info": "ℹ️"}
    order = {"Fail": 0, "Warning": 1, "Info": 2, "Pass": 3}
    df = df.assign(_o=df["Result"].map(order)).sort_values("_o", kind="stable").drop(columns="_o")
    df.insert(0, "", df["Result"].map(icon))
    df = df.drop(columns=["Result", "What to do"])
    st.dataframe(df, hide_index=True, width="stretch", height=38 + 35 * len(df),
                 column_config={"": st.column_config.TextColumn(width=40), "Area": st.column_config.TextColumn(width=120),
                                "Check": st.column_config.TextColumn(width=260),
                                "What we found": st.column_config.TextColumn(width=520)})
    with st.expander("Usage batch tie-out (after de-duplication)"):
        up = r.usage_prep
        agg = up["clean"].groupby("batch_id").agg(records=("record_id", "nunique"), runs=("units_i", "sum")).reset_index()
        ctl = r.tables["batch_controls"][["batch_id", "service_date", "distinct_active_record_count", "active_units_total"]].copy()
        ctl["distinct_active_record_count"] = ctl["distinct_active_record_count"].astype(int)
        ctl["active_units_total"] = ctl["active_units_total"].astype(int)
        m = ctl.merge(agg, on="batch_id", how="outer")
        m["Ties"] = ((m["records"] == m["distinct_active_record_count"]) & (m["runs"] == m["active_units_total"])).map({True: "✅", False: "❌"})
        m = m.rename(columns={"batch_id": "Batch", "service_date": "Date", "distinct_active_record_count": "Control records",
                              "active_units_total": "Control runs", "records": "File records", "runs": "File runs"})
        st.dataframe(m, hide_index=True, width="stretch")


def _short(name):
    return re.sub(r",? Inc\.?$", "", str(name))


def _prior():
    """Prior close snapshot: per-customer rows and invoices (empty frames if none)."""
    snap = ss.baseline.get("snapshot", {}) if ss.get("baseline") else {}
    return snap.get("customers", pd.DataFrame()), snap.get("invoices", pd.DataFrame())


def _current_rows(r):
    out = {}
    rf = {(x["ns_id"], x["account"]): x for x in r.rollforward}
    for c in r.customers:
        u, p = r.usage.get(c.ns_id, {}), r.platform.get(c.ns_id, {})
        out[c.ns_id] = dict(usage_runs=u.get("units", 0), allowance=u.get("allowance", 0), excess=u.get("excess", 0),
                            usage_expected=float(u.get("net", 0)), usage_billed=float(u.get("billed_net", 0)),
                            usage_accrued=float(u.get("accrued", 0)), platform_expected=float(p.get("expected_cover", 0)),
                            platform_billed=float(p.get("actual_cover", 0)), platform_revenue=float(p.get("gl_rev", 0)),
                            **{f"bal_{a}": float(rf[(c.ns_id, a)]["gl_close"]) for a in ("1100", "1150", "1160", "2200") if (c.ns_id, a) in rf})
    return out


def page_summary():
    st.title("Customer Summary")
    prior, _ = _prior()
    r = ss.result
    plabel = prior["period"].iloc[0] if len(prior) else "Prior month"
    views = ([r.period_label, plabel, "Change"] if r is not None else [plabel])
    view = st.radio("Period", views, horizontal=True, label_visibility="collapsed",
                    format_func=lambda v: f"{plabel} → {r.period_label}" if v == "Change" else v)
    acc = st.column_config.NumberColumn(format="accounting")

    if view == plabel:
        if not len(prior):
            st.info("No prior close loaded.")
            return
        c = st.columns(4)
        c[0].metric("Platform billed", fmt_usd(prior["platform_billed"].sum()))
        c[1].metric("Platform revenue", fmt_usd(prior["platform_revenue"].sum()))
        c[2].metric("Usage billed", fmt_usd(prior["usage_billed"].sum()))
        c[3].metric("Unbilled usage (1150)", fmt_usd(prior["bal_1150"].sum()))
        df = pd.DataFrame({"Customer": prior["customer"].map(_short), "Status": "✅ Closed", "Billing": prior["billing"],
                           "Platform billed": prior["platform_billed"], "Platform revenue": prior["platform_revenue"],
                           "Usage runs": prior["usage_runs"], "Usage billed": prior["usage_billed"],
                           "Usage accrued": prior["usage_accrued"], "Unbilled usage (1150)": prior["bal_1150"]})
        st.dataframe(df, hide_index=True, width="stretch", height=38 + 35 * len(df),
                     column_config={**{k: acc for k in df.columns[3:]}, "Usage runs": st.column_config.NumberColumn(format="localized")})
        if r is None:
            st.page_link(PAGE_START, label=f"Load {calendar.month_name[ss.month]} files to compare →", icon="📥")
        return

    t = totals(r)
    cur = _current_rows(r)
    if view == "Change":
        if not len(prior):
            st.info("No prior close loaded.")
            return
        pr = prior.set_index("ns_id")
        rows = []
        for cst in r.customers:
            a, b = (pr.loc[cst.ns_id] if cst.ns_id in pr.index else None), cur[cst.ns_id]
            g = (lambda k: float(a[k]) if a is not None else 0.0)
            rows.append({"Customer": _short(cst.name),
                         f"Runs {plabel[:3]}": g("usage_runs"), f"Runs {r.period_label[:3]}": b["usage_runs"],
                         "Runs Δ": b["usage_runs"] - g("usage_runs"),
                         f"Usage {plabel[:3]}": g("usage_expected"), f"Usage {r.period_label[:3]}": b["usage_expected"],
                         "Usage Δ": b["usage_expected"] - g("usage_expected"),
                         f"Platform rev {plabel[:3]}": g("platform_revenue"), f"Platform rev {r.period_label[:3]}": b["platform_revenue"],
                         "Platform rev Δ": b["platform_revenue"] - g("platform_revenue")})
        df = pd.DataFrame(rows)
        c = st.columns(3)
        c[0].metric("Usage runs", f"{int(df.iloc[:, 2].sum()):,}", f"{int(df['Runs Δ'].sum()):+,} vs {plabel[:3]}")
        c[1].metric("Usage charges (contract)", fmt_usd(df.iloc[:, 5].sum()), fmt_usd(df["Usage Δ"].sum(), True) + f" vs {plabel[:3]}")
        c[2].metric("Platform revenue (GL)", fmt_usd(df.iloc[:, 8].sum()), fmt_usd(df["Platform rev Δ"].sum(), True) + f" vs {plabel[:3]}")
        cfg = {k: acc for k in df.columns[4:]}
        cfg.update({k: st.column_config.NumberColumn(format="localized") for k in df.columns[1:4]})
        st.dataframe(df, hide_index=True, width="stretch", height=38 + 35 * len(df), column_config=cfg)
        return

    c = st.columns(4)
    sp = prior["platform_billed"].sum() if len(prior) else None
    su = prior["usage_billed"].sum() if len(prior) else None
    c[0].metric("Platform billing – expected", fmt_usd(t["expected_platform"]),
                fmt_usd(t["actual_platform"] - t["expected_platform"], True) + " vs actual", delta_color="off")
    c[1].metric("Usage billing – expected", fmt_usd(t["expected_usage"]),
                fmt_usd(t["billed_usage"] - t["expected_usage"], True) + " vs actual", delta_color="off")
    c[2].metric("Under-billed", fmt_usd(t["under_billed"]))
    c[3].metric("Over-billed", fmt_usd(t["over_billed"]))
    full = customer_summary(r)
    df = pd.DataFrame({
        "Customer": full["Customer"].map(_short), "Status": full["Status"],
        "Platform expected": full["Platform: expected invoices covering month"],
        "Platform billed": full["Platform: actual invoices covering month"],
        "Platform diff": full["Platform billing difference"],
        "Usage expected": full["Usage: expected (net)"], "Usage billed": full["Usage: billed (net of credits)"],
        "Usage diff": full["Usage billing difference"],
    })
    if st.toggle("Exceptions only"):
        df = df[df["Status"] != "✅ Ties"]
    st.dataframe(df, hide_index=True, width="stretch", height=38 + 35 * max(len(df), 1),
                 column_config={c_: acc for c_ in df.columns[2:]})


def _month_name(label):
    """'Sep 2026' -> 'September 2026'."""
    try:
        return datetime.strptime(label, "%b %Y").strftime("%B %Y")
    except ValueError:
        return label


def _section(title):
    st.markdown(f"##### {title}")


def _table(rows, columns):
    df = pd.DataFrame(rows, columns=columns)
    widths = {"Metric": 175, "Calculation": 240, "Value": 105, "Source": 190, "Notes": 260, "Field": 200, "Value ": 300}
    cfg = {c_: st.column_config.TextColumn(width=widths.get(c_)) if c_ in widths else st.column_config.TextColumn() for c_ in columns}
    st.dataframe(df, hide_index=True, width="stretch", height=38 + 35 * max(len(df), 1), column_config=cfg)


METRIC_COLS = ["Metric", "Calculation", "Value", "Source", "Notes"]


def _diff_note(actual, expected):
    d = Decimal(str(actual)) - Decimal(str(expected))
    if abs(d) < Decimal("0.005"):
        return "Ties to expected"
    return f"{'Over' if d > 0 else 'Under'} expected by {fmt_usd(abs(d))}"


def _usage_rows_current(u, k):
    rows = []
    recs, late = u.get("records"), u.get("late")
    accts = u.get("by_account", {})
    rows.append(("Usage runs", f"Sum of {len(recs) if recs is not None else 0} unique daily records",
                 fmt_int(u.get("units", 0)), "product_usage.csv",
                 " · ".join(f"{pa}: {fmt_int(n)}" for pa, n in accts.items()) if len(accts) > 1 else
                 (f"{len(late)} record(s) ingested after the invoice" if late is not None and len(late) else "")))
    segs = u.get("segments", [])
    if segs and len(segs) == 1 and segs[0][1] == sum(x[1] for x in segs) and u.get("allowance") == segs[0][0]:
        calc, note = "Full month", ""
    else:
        dim = (pd.Timestamp(segs[0][3]).days_in_month if segs else 30)
        calc = " + ".join(f"{fmt_int(a)} × {n}" for a, n, *_ in segs) + f" ÷ {dim} days"
        note = "Day-weighted, rounded once" if len(segs) > 1 else "First partial month, prorated"
    rows.append(("Included allowance", calc, fmt_int(u.get("allowance", 0)), " / ".join(dict.fromkeys(s[2] for s in segs)), note))
    rows.append(("Excess runs", f"max(0, {fmt_int(u.get('units', 0))} − {fmt_int(u.get('allowance', 0))})", fmt_int(u.get("excess", 0)), "Calculated", ""))
    rows.append(("Overage rate", "", f"${u.get('rate', 0)}", u.get("rate_doc", ""), ""))
    rows.append(("Usage charge", f"{fmt_int(u.get('excess', 0))} × ${u.get('rate', 0)}", fmt_usd(u.get("gross", 0)), "Calculated", ""))
    if u.get("concession"):
        rows.append(("Approved concession", "", f"({fmt_usd(u['concession'])})", ", ".join(u.get("concession_refs", [])), "Controller approved"))
    rows.append(("Expected usage", "Charge less approved concessions" if u.get("concession") else "", fmt_usd(u.get("net", 0)), "Calculated", ""))
    bl = u.get("billed_lines")
    calc = "; ".join(f"{fmt_int(x['qty'])} × {x['unit_amount_decimal']}¢" for x in bl.to_dict("records")) if bl is not None and len(bl) else "No invoice"
    src = ", ".join(bl["number"]) if bl is not None and len(bl) else "Stripe"
    if u.get("billed_credit"):
        calc += f"; less credit notes {fmt_usd(u['billed_credit'])}"
        src += ", " + ", ".join(u["credits"]["number"])
    rows.append(("Billed", calc, fmt_usd(u.get("billed_net", 0)), src, _diff_note(u.get("billed_net", 0), u.get("net", 0))))
    al = u.get("accrual_lines")
    rows.append(("Accrued (4010)", "GL usage revenue for the month", fmt_usd(u.get("accrued", 0)),
                 ", ".join(al["Document Number"]) if al is not None and len(al) else "NetSuite GL – none posted",
                 _diff_note(u.get("accrued", 0), u.get("net", 0))))
    return rows


def _usage_rows_prior(pr, k, iv):
    rows = [("Usage runs", "", fmt_int(int(pr["usage_runs"])), "product_usage.csv", "")]
    base = int(D(k.order.get("included_runs_per_month"))) if k else 0
    alw = int(pr["allowance"])
    rows.append(("Included allowance", "Full month" if alw == base else f"{fmt_int(base)} × active days ÷ days in month",
                 fmt_int(alw), k.order.document_id if k else "", "" if alw == base else "First partial month, prorated"))
    rows.append(("Excess runs", f"max(0, {fmt_int(int(pr['usage_runs']))} − {fmt_int(alw)})", fmt_int(int(pr["excess"])), "Calculated", ""))
    rows.append(("Overage rate", "", f"${float(pr['rate']):.2f}", k.order.document_id if k else "", ""))
    rows.append(("Usage charge", f"{fmt_int(int(pr['excess']))} × ${float(pr['rate']):.2f}", fmt_usd(pr["usage_expected"]), "Calculated", ""))
    ul = iv[iv["product"] == "prod_workflow_run"] if len(iv) and "product" in iv else iv.iloc[0:0] if len(iv) else iv
    rows.append(("Billed", "; ".join(f"{fmt_int(int(x['qty']))} × {x['unit_cents']}¢" for x in ul.to_dict("records")) or "No invoice",
                 fmt_usd(pr["usage_billed"]), ", ".join(ul["invoice"]) if len(ul) else "Stripe",
                 _diff_note(pr["usage_billed"], pr["usage_expected"])))
    rows.append(("Accrued (4010)", "Unbilled usage at month end", fmt_usd(pr["usage_accrued"]), "Opening balances (1150)",
                 _diff_note(pr["usage_accrued"], pr["usage_expected"])))
    return rows


def _platform_rows(k, rec, booked, booked_src, plan=None, plan_src="", inv_rows=()):
    rows = []
    if k:
        rows.append(("Annual platform fee", "", fmt_usd(D(k.order.get("platform_annual_fee"))), k.order.document_id,
                     f"{k.billing_frequency} billing in advance"))
        for a in k.amendments:
            if a.has("platform_annual_fee") and any(r_["doc"] == a.document_id for r_ in rec):
                rows.append(("Annual platform fee (amended)", "", fmt_usd(D(a.get("platform_annual_fee"))), a.document_id,
                             f"Effective {a.effective_from}"))
    for x in rec:
        rows.append((f"Revenue – {'upgrade' if x['layer'].startswith('Upgrade') else 'base'}", x["calc"].split("; ")[0],
                     fmt_usd(x["amount"]), x["doc"], "Final period absorbs rounding" if "final period" in x["calc"] else ""))
    expected = sum((x["amount"] for x in rec), ZERO)
    rows.append(("Expected revenue", "Sum of layers" if len(rec) > 1 else "", fmt_usd(expected), "Calculated", ""))
    if plan is not None:
        rows.append(("Revenue plan", "", fmt_usd(plan), plan_src, _diff_note(plan, expected)))
    rows.append(("Recognized (4000)", "", fmt_usd(booked), booked_src, _diff_note(booked, expected)))
    for label, period, amount, src, note in inv_rows:
        rows.append((label, period, amount, src, note))
    return rows


def _unit_price(product, cents):
    if not str(cents).strip():
        return "–"
    v = D(cents) / 100
    return fmt_usd(v) if product == "prod_platform" else f"${v:.2f} / run"


def _customer_data(name, ns, sf, stripe):
    k_ = (ss.result.contracts if ss.result is not None else build_contracts(ss.terms, ss.tables["accounts"])[0]).get(sf)
    contract_id = k_.contract_id if k_ else "–"
    _section("Contract(s) & Entitlements")
    _contract_tab(sf)
    acc_t = ss.tables["accounts"]
    a = acc_t[acc_t["AccountId"] == sf].iloc[0]
    _section("Identifiers")
    _table([("Customer", a["AccountName"], "Salesforce"), ("Salesforce account ID", sf, "Salesforce"),
            ("Account owner", a["AccountOwner"] or "–", "Salesforce"), ("Billing country", a["BillingCountry"] or "–", "Salesforce"),
            ("Parent account", a["ParentId"] or "–", "Salesforce"),
            ("Stripe customer ID", stripe, "Salesforce → Stripe (StripeCustomerId__c)"),
            ("NetSuite customer ID", ns, "Salesforce → NetSuite (NetSuiteCustomerInternalId__c)"),
            ("Contract ID", contract_id, "Contract documents")],
           ["Field", "Value", "Source"])

    _section("Product accounts")
    mp = ss.tables["mapping"]
    pas = mp[mp["salesforce_account_id"] == sf]
    usage = ss.tables.get("usage")
    sku = (lambda pa: ", ".join(sorted(set(usage.loc[usage["product_account_id"] == pa, "sku"]))) or "WORKFLOW_RUN") if usage is not None else (lambda pa: "WORKFLOW_RUN")
    if len(pas):
        _table([(x["product_account_id"], x["account_label"], sku(x["product_account_id"]), x["effective_from"],
                 x["effective_to"] or "Open", "Verified") for x in pas.to_dict("records")],
               ["Product account ID", "Label", "SKU", "Effective from", "Effective to", "Status"])
    else:
        st.warning("No verified product account – usage can't be attributed to this customer.")

    _section("Billing products (Stripe)")
    if ss.result is not None:
        li = ss.result.tables["invoice_lines"].merge(ss.result.tables["invoices"], left_on="invoice_id", right_on="id", suffixes=("", "_inv"))
        li = li[li["customer_id"] == stripe].sort_values("created")
        prods = [(pid, price, _unit_price(pid, li[(li['price_id'] == price)]['unit_amount_decimal'].iloc[-1]),
                  str(len(li[li["price_id"] == price]))) for (pid, price) in li[["product_id", "price_id"]].drop_duplicates().itertuples(index=False)]
    else:
        _, iv = _prior()
        iv = iv[iv["ns_id"] == ns] if len(iv) else iv
        prods = [(x["product"], x["price_id"], _unit_price(x["product"], x["unit_cents"]) if x["unit_cents"] else fmt_usd(x["amount"]), "1")
                 for x in iv.drop_duplicates("price_id").to_dict("records")] if len(iv) and "price_id" in iv else []
    if prods:
        _table(prods, ["Stripe product", "Price ID", "Latest unit price", "Invoice lines"])


def page_customer():
    st.title("Customer Detail")
    r = ss.result
    prior, prior_inv = _prior()
    plabel = prior["period"].iloc[0] if len(prior) else "Prior month"
    acc_t = ss.tables["accounts"]
    customers = list(zip(acc_t["AccountName"], acc_t["NetSuiteCustomerInternalId__c"], acc_t["AccountId"], acc_t["StripeCustomerId__c"]))
    flagged = {f.customer for f in r.findings if f.classification == ERROR} if r is not None else set()
    names = sorted([c[0] for c in customers], key=lambda n: (n not in flagged, n))
    if ss.get("detail_cust") not in names:
        ss.detail_cust = names[0]
    name = st.selectbox("Customer", names, format_func=lambda n: ("🔴 " if n in flagged else "") + n, label_visibility="collapsed",
                        key="detail_cust")
    _, ns, sf, stripe = [c for c in customers if c[0] == name][0]
    pr = prior[prior["ns_id"] == ns].iloc[0] if len(prior) and (prior["ns_id"] == ns).any() else None
    iv = prior_inv[prior_inv["ns_id"] == ns] if len(prior_inv) else prior_inv
    contracts = r.contracts if r is not None else build_contracts(ss.terms, ss.tables["accounts"])[0]
    k = contracts.get(sf)

    months = ([r.period_label] if r is not None else []) + ([plabel] if pr is not None else [])
    show_trends = r is not None and pr is not None
    fs = [f for f in r.findings if f.ns_id == ns] if r is not None else []
    has_err = any(f.classification == ERROR for f in fs)
    ms_label = ("🔴 " if has_err else "") + "Monthly Summary"
    want = ms_label if ss.pop("detail_tab", None) == "Monthly Summary" else None
    tabs = st.tabs(["Customer Data", ms_label] + (["Customer Trends"] if show_trends else []), default=want)
    if show_trends:
        with tabs[2]:
            cur = _current_rows(r)[ns]
            lines = [("Usage runs", "usage_runs", "n"), ("Allowance", "allowance", "n"), ("Excess runs", "excess", "n"),
                     ("Usage charge (contract)", "usage_expected", "$"), ("Usage billed", "usage_billed", "$"),
                     ("Usage accrued (4010)", "usage_accrued", "$"), ("Platform revenue (4000)", "platform_revenue", "$"),
                     ("AR (1100)", "bal_1100", "$"), ("Unbilled usage (1150)", "bal_1150", "$"),
                     ("Contract asset (1160)", "bal_1160", "$"), ("Deferred revenue (2200)", "bal_2200", "$")]
            fmt = lambda v, kind: f"{int(v):,}" if kind == "n" else (fmt_usd(v) if v >= 0 else f"({fmt_usd(-v)})")
            cmp = pd.DataFrame([{"": lab, plabel: fmt(float(pr[key]), kind), r.period_label: fmt(float(cur.get(key, 0)), kind),
                                 "Change": fmt(float(cur.get(key, 0)) - float(pr[key]), kind)} for lab, key, kind in lines])
            st.dataframe(cmp, hide_index=True, width="stretch", height=38 + 35 * len(cmp),
                         column_config={c_: st.column_config.TextColumn(width="medium") for c_ in cmp.columns})


    with tabs[0]:
        _customer_data(name, ns, sf, stripe)
    with tabs[1]:
        if not months:
            st.info("No data for this customer.")
            return
        month = st.selectbox("Month", months, label_visibility="collapsed", key=f"month_{ns}",
                             format_func=lambda m_: ("🔴 " if has_err and r is not None and m_ == r.period_label else "") + _month_name(m_))
        if r is not None and month == r.period_label and fs:
            st.markdown("  \n".join(f"{CLS_ICON[f.classification]} **{f.id}** {f.title}" for f in fs))
        m0 = datetime.strptime(month, "%b %Y").date()
        m1 = (pd.Timestamp(m0) + pd.offsets.MonthBegin(1)).date()
        current = r is not None and month == r.period_label
        if current:
            u, p = r.usage.get(ns, {}), r.platform.get(ns, {})
            _section("Usage")
            _table(_usage_rows_current(u, k), METRIC_COLS)
            recs = u.get("records")
            if recs is not None and len(recs):
                with st.expander(f"Daily usage records ({len(recs)})"):
                    st.dataframe(recs[["record_id", "product_account_id", "service_date", "units", "ingested_at", "batch_id"]],
                                 hide_index=True, width="stretch")
            if p:
                _section("Platform")
                inv_rows = [(x["kind"], f"Service {x['period_start']} – {x['period_end'] - pd.Timedelta(days=1)}",
                             fmt_usd(x["actual"]), x["invoice"] or "Not billed",
                             "Ties to contract" if x["expected"] == x["actual"] else f"Contract {fmt_usd(x['expected'])}")
                            for x in p["rows"]]
                _table(_platform_rows(k, p["recognition"], p["gl_rev"], "NetSuite GL", p["planned"],
                                      "Revenue plan (" + ", ".join(p["plan_docs"]) + ")", inv_rows), METRIC_COLS)
            _section("Invoices")
            li = r.tables["invoice_lines"].merge(r.tables["invoices"], left_on="invoice_id", right_on="id", suffixes=("", "_inv"))
            li = li[li["customer_id"] == stripe].copy()
            li["ps"] = li["period_start"].map(lambda x: parse_date(x))
            li["pe"] = li["period_end"].map(lambda x: parse_date(x))
            li = li[(li["ps"] < m1) & (li["pe"] > m0)].sort_values("created")
            show_df(pd.DataFrame({"Invoice": li["number"], "Issued": li["created"].str[:10], "Line": li["description"],
                                  "Qty": li["quantity"].astype(int),
                                  "Unit price": [_unit_price(pid, c) for pid, c in zip(li["product_id"], li["unit_amount_decimal"])],
                                  "Amount": li["amount"].astype(int) / 100, "Service period": li["ps"].astype(str) + " – " +
                                  li["pe"].map(lambda d: str(d - pd.Timedelta(days=1))), "Status": li["status"]}))
            cn = r.tables["credit_notes"][r.tables["credit_notes"]["customer_id"] == stripe]
            if len(cn):
                st.dataframe(pd.DataFrame({"Credit note": cn["number"], "Created": cn["created"].str[:10], "Status": cn["status"],
                                           "Amount": cn["amount"].astype(int) / 100}), hide_index=True, width="stretch",
                             column_config={"Amount": MONEY})
            _section("Month End Balances")
            rf = [x for x in r.rollforward if x["ns_id"] == ns and x["account"] in ("1100", "1150", "1160", "2200")]
            bal = pd.DataFrame([{"GL account": x["account"], "Account name": x["account_name"], "Opening": float(x["opening"]),
                                 "Activity": float(x["activity"]), "Close per GL": float(x["gl_close"]),
                                 "Proposed adjustments": float(x["proposed"]), "Corrected close": float(x["corrected_close"])} for x in rf])
            acc = st.column_config.NumberColumn(format="accounting")
            st.dataframe(bal, hide_index=True, width="stretch", height=38 + 35 * len(bal),
                         column_config={c_: acc for c_ in bal.columns[2:]})
            with st.expander("GL lines"):
                g = r.tables["gl"]
                st.dataframe(g[g["Customer Internal ID"] == ns], hide_index=True, width="stretch")
        else:
            _section("Usage")
            _table(_usage_rows_prior(pr, k, iv), METRIC_COLS)
            _section("Platform")
            pl = iv[iv["product"] == "prod_platform"] if len(iv) and "product" in iv else iv.iloc[0:0] if len(iv) else iv
            inv_rows = [(x["line"], f"Service from {x['service_from']}", fmt_usd(x["amount"]), x["invoice"], "")
                        for x in pl.to_dict("records")]
            rec = recognition_rows(k, m0, m1) if k else []
            _table(_platform_rows(k, rec, D(str(pr["platform_revenue"])), "Revenue plan (recognized)", None, "", inv_rows), METRIC_COLS)
            _section("Invoices")
            if len(iv):
                st.dataframe(pd.DataFrame({"Invoice": iv["invoice"], "Issued": iv["issued"], "Line": iv["line"],
                                           "Amount": iv["amount"], "Service from": iv["service_from"]}),
                             hide_index=True, width="stretch", column_config={"Amount": MONEY})
            _section("Month End Balances")
            bal = pd.DataFrame([{"GL account": a, "Account name": ACCOUNTS[a], f"{_month_name(month)} close": float(pr[f"bal_{a}"])}
                                for a in ("1100", "1150", "1160", "2200")])
            st.dataframe(bal, hide_index=True, width="stretch",
                         column_config={f"{_month_name(month)} close": st.column_config.NumberColumn(format="accounting")})


BILLING_ICON = {"Reflected": "✅ Yes", "Not reflected": "❌ No", "Corrected": "🟠 Corrected",
                "Not yet billed": "⏳ Not yet billed", "Not yet effective": "– Not yet effective"}


def billing_status(doc_id, doc_type=None, customer_name=None):
    """Repository attribute: when the document was loaded to billing (recorded once from the contract register)."""
    m = ss.terms.loc[ss.terms["document_id"] == doc_id, "billing_loaded_at"]
    v = str(m.iloc[0]).strip() if len(m) else ""
    return f"✅ {v[:10]}" if v else "❌ No"


def _doc_status(d, customer_name=""):
    signed = long_date(d.executed) if d.executed else "–"
    return f"Executed {signed} · Loaded to billing: {billing_status(d.document_id, d.document_type, customer_name)}"


def _payment_terms(doc_id):
    data = ss.pdfs.get(f"{doc_id}.pdf")
    if data:
        m = re.search(r"in advance (Net \d+ days / [A-Z]{3})", pdf_text(data))
        if m:
            return m.group(1)
    return "Net 30 days / USD"


def _prev_value(k, a, field):
    v = k.order.get(field)
    for b in k.amendments:
        if b is a:
            break
        if b.has(field):
            v = b.get(field)
    return v


def _doc_header(doc_id, label):
    """Document ID as a link that opens the executed PDF (same pop-up as Contracts & Terms)."""
    with st.container(key=f"doclink_{doc_id}"):
        c1, c2 = st.columns([1.4, 8], vertical_alignment="center")
        if c1.button(doc_id, key=f"open_doc_{doc_id}", type="tertiary"):
            contract_dialog(doc_id)
        c2.markdown(label)


def _contract_tab(sf_id):
    contracts = ss.result.contracts if ss.result is not None else build_contracts(ss.terms, ss.tables["accounts"])[0]
    k = contracts.get(sf_id)
    if not k:
        st.info("No contract on file.")
        return
    d, freq = k.order, k.billing_frequency
    annual, inv = D(d.get("platform_annual_fee")), D(d.get("platform_invoice_amount"))
    fee = f"{fmt_usd(annual)} / year" if freq.lower().startswith("annual") else f"{fmt_usd(inv)} / month"

    cname = ss.tables["accounts"].set_index("AccountId").loc[sf_id, "AccountName"]
    _doc_header(d.document_id, f"Order form · {k.contract_id}")
    st.caption(_doc_status(d, cname))
    _table([(long_date(k.service_start), long_date(k.service_end_excl - pd.Timedelta(days=1)),
             f"{freq} in advance", _payment_terms(d.document_id))],
           ["Service start", "Service end", "Billing", "Payment / currency"])
    _table([("RelayCloud Platform", "Annual subscription", f"{freq}, in advance", fee),
            ("Workflow runs", f"{int(D(d.get('included_runs_per_month'))):,} runs / month", "Monthly, in arrears",
             f"${D(d.get('overage_rate_per_run')):.2f} / excess run"),
            ("Annual commitment", "", "", f"{fmt_usd(annual)} + overages")],
           ["Product", "Entitlement", "Billing basis", "Fees (USD)"])

    for a in k.amendments:
        rows = []
        if a.has("included_runs_per_month"):
            rows.append(("Included workflow runs", f"{int(D(_prev_value(k, a, 'included_runs_per_month'))):,} / month",
                         f"{int(D(a.get('included_runs_per_month'))):,} / month"))
        if a.has("overage_rate_per_run"):
            rows.append(("Overage rate", f"${D(_prev_value(k, a, 'overage_rate_per_run')):.2f} / run",
                         f"${D(a.get('overage_rate_per_run')):.2f} / run"))
        if a.has("platform_annual_fee"):
            rows.append(("Annual platform rate", f"{fmt_usd(D(_prev_value(k, a, 'platform_annual_fee')))} / year",
                         f"{fmt_usd(D(a.get('platform_annual_fee')))} / year"))
        if a.has("platform_invoice_amount"):
            frm = f" from {long_date(parse_date(a.get('invoice_amount_effective_from')))}" if a.has("invoice_amount_effective_from") else ""
            rows.append(("Platform invoice", fmt_usd(D(_prev_value(k, a, "platform_invoice_amount"))),
                         f"{fmt_usd(D(a.get('platform_invoice_amount')))}{frm}"))
        if a.has("catch_up_charge"):
            rows.append(("Upgrade catch-up (one-time)", "Not previously billed", f"{fmt_usd(D(a.get('catch_up_charge')))} additional"))
        if a.has("incremental_consideration"):
            rows.append(("Incremental consideration", "–", f"{fmt_usd(D(a.get('incremental_consideration')))} over remaining term"))
        _doc_header(a.document_id, f"Amendment to {d.document_id}, effective {long_date(a.effective_from)}")
        st.caption(_doc_status(a, cname))
        _table(rows, ["Term", "Previous", "Amended"])


def _var(actual, expected, kind="$"):
    d = Decimal(str(actual)) - Decimal(str(expected))
    if abs(d) < Decimal("0.005"):
        return "–"
    if kind == "n":
        return f"{int(d):+,}"
    return fmt_usd(d, True)


def _first_sentence(text):
    m = re.match(r"(.+?[.!?])(\s|$)", text or "")
    return m.group(1) if m else (text or "")


def _xlsx(df: pd.DataFrame, highlight_mask, sheet: str) -> bytes:
    """Excel with issue rows highlighted (yellow) – for bridging back to the source system."""
    from openpyxl.styles import Font, PatternFill
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name=sheet, index=False)
        ws = xw.sheets[sheet]
        fill = PatternFill("solid", fgColor="FFF2A8")
        for i, flag in enumerate(list(highlight_mask), start=2):
            if flag:
                for cell in ws[i]:
                    cell.fill = fill
                    cell.font = Font(bold=True)
        for i, col in enumerate(df.columns, 1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = min(40, max(12, len(str(col)) + 2))
        ws.freeze_panes = "D2"
    return buf.getvalue()


def _ids_frame(cust, n):
    return pd.DataFrame({"Customer Name": [cust.name] * n, "Salesforce ID": [cust.sf_id] * n,
                         "Stripe customer ID": [cust.stripe_id] * n})


def dl_invoice_lines(cust, highlight_ids=()):
    r = ss.result
    li = r.tables["invoice_lines"].merge(r.tables["invoices"], left_on="invoice_id", right_on="id", suffixes=("", "_invoice"))
    li = li[li["customer_id"] == cust.stripe_id].sort_values(["created", "number"]).reset_index(drop=True)
    li = li.assign(**{"Unit price ($)": li["unit_amount_decimal"].map(lambda c: float(D(c) / 100)),
                      "Line amount ($)": li["amount"].map(lambda c: float(D(c) / 100))})
    cols = ["number", "created", "status", "id", "description", "product_id", "price_id", "quantity", "unit_amount_decimal",
            "Unit price ($)", "amount", "Line amount ($)", "period_start", "period_end", "proration", "invoice_id"]
    out = pd.concat([_ids_frame(cust, len(li)), li[cols].rename(columns={"number": "invoice_number", "id": "line_id"})], axis=1)
    return _xlsx(out, li["id"].isin(set(highlight_ids)), "Stripe invoice lines")


def dl_usage(cust, highlight_ids=()):
    r = ss.result
    mp = r.tables["mapping"]
    pas = set(mp.loc[mp["salesforce_account_id"] == cust.sf_id, "product_account_id"])
    us = r.usage_prep["clean"]
    us = us[us["product_account_id"].isin(pas)].sort_values(["service_date_d", "product_account_id"]).reset_index(drop=True)
    cols = ["record_id", "product_account_id", "sku", "service_date", "units", "ingested_at", "record_status", "batch_id"]
    out = pd.concat([_ids_frame(cust, len(us)), us[cols]], axis=1)
    return _xlsx(out, us["record_id"].isin(set(highlight_ids)), "Usage records")


def dl_gl(cust, highlight_docs=()):
    r = ss.result
    g = r.tables["gl"]
    g = g[g["Customer Internal ID"] == cust.ns_id].reset_index(drop=True)
    out = pd.concat([_ids_frame(cust, len(g)), g], axis=1)
    return _xlsx(out, g["Document Number"].isin(set(highlight_docs)), "GL lines")


def _variance_rows(f):
    """Rows: dict(metric, expected, actual, var, key, source=[parts]).
    Source parts: ("text", s) | ("doc", doc_id) | ("dl", label, fn, highlight)."""
    r = ss.result
    if f.classification != ERROR:
        return None
    cust = next((c for c in r.customers if c.ns_id == f.ns_id), None)
    k = r.contracts.get(cust.sf_id) if cust else None
    INV = lambda hl=(): ("dl", "Stripe invoice lines", "inv", tuple(hl))
    if f.area == "Usage" and f.ns_id in r.usage:
        u = r.usage[f.ns_id]
        bl = u["billed_lines"]
        main = bl[bl["proration"].str.lower() != "true"] if len(bl) else bl
        rate_bad = main[main["unit_usd"] != u["rate"]] if len(main) else main
        units = sorted(set(main["unit_usd"])) if len(main) else []
        unrefl = [a for a in (k.amendments if k else []) if a.billing_status in ("Not reflected", "Corrected")
                  and (a.has("included_runs_per_month") or a.has("overage_rate_per_run"))]
        late = list(u["late"]["record_id"]) if len(u.get("late", [])) else []
        all_ids = list(bl["id"]) if len(bl) else []
        if unrefl:
            runs_src = [("doc", unrefl[0].document_id), ("text", "vs"), INV(main["id"])]
        elif late:
            runs_src = [("dl", "Usage records", "usage", tuple(late)), ("text", "vs"), INV(main["id"])]
        elif not len(main):
            runs_src = [("dl", "Usage records", "usage", ()), ("text", "vs no invoice")]
        else:
            runs_src = [("dl", "Usage records", "usage", ()), ("text", "vs"), INV(main["id"])]
        al = u["accrual_lines"]
        rows = [
            dict(metric="Billable runs", expected=fmt_int(u["excess"]), actual=fmt_int(u["billed_qty"]),
                 var=_var(u["billed_qty"], u["excess"], "n"), key=not len(rate_bad) and u["billed_qty"] != u["excess"],
                 source=runs_src),
            dict(metric="Rate per run", expected=f"${u['rate']:.2f}", actual=", ".join(f"${x:.2f}" for x in units) or "–",
                 var="–" if not units or units == [u["rate"]] else _var(units[0], u["rate"]), key=bool(len(rate_bad)),
                 source=[("doc", u.get("rate_doc", "")), ("text", "vs"), INV(rate_bad["id"] if len(rate_bad) else ())]),
            dict(metric="Usage charge", expected=fmt_usd(u["net"]), actual=fmt_usd(u["billed_net"]),
                 var=_var(u["billed_net"], u["net"]), key=False, source=[INV(all_ids)] if all_ids else [("text", "No invoice")]),
            dict(metric="Accrued (4010)", expected=fmt_usd(u["net"]), actual=fmt_usd(u["accrued"]),
                 var=_var(u["accrued"], u["net"]), key=False,
                 source=[("dl", "GL lines", "gl", tuple(al["Document Number"]) if len(al) else ())]),
        ]
        return rows
    if f.area == "Platform" and f.ns_id in r.platform:
        p = r.platform[f.ns_id]
        rows = []
        for x in p["rows"]:
            rows.append(dict(metric=x["kind"], expected=fmt_usd(x["expected"]), actual=fmt_usd(x["actual"]),
                             var=_var(x["actual"], x["expected"]), key=x["expected"] != x["actual"],
                             source=[("doc", x["source"]), ("text", "vs"),
                                     INV([x["line_id"]] if x["line_id"] else ()) if x["invoice"] else ("text", "not billed")]))
        rev_docs = tuple(p["plan_rows"]["Journal Internal ID"]) if len(p["plan_rows"]) else ()
        g = r.tables["gl"]
        rev_doc_nums = tuple(g.loc[(g["Customer Internal ID"] == f.ns_id) & (g["Account Number"] == "4000"), "Document Number"])
        rows.append(dict(metric="Recognized revenue (4000)", expected=fmt_usd(p["expected_rev"]), actual=fmt_usd(p["gl_rev"]),
                         var=_var(p["gl_rev"], p["expected_rev"]), key=p["gl_rev"] != p["expected_rev"],
                         source=[("dl", "GL lines", "gl", rev_doc_nums)]))
        rows.append(dict(metric="Revenue plan", expected=fmt_usd(p["expected_rev"]), actual=fmt_usd(p["planned"]),
                         var=_var(p["planned"], p["expected_rev"]), key=False,
                         source=[("text", "Plan from"), *[("doc", d) for d in p["plan_docs"]]]))
        return rows
    return None


VGRID_W = [1.5, 1.0, 1.0, 1.0, 3.2]


def _render_variance_grid(f, rows, cust):
    with st.container(key=f"vgrid_{f.id}"):
        with st.container(key=f"vhd_{f.id}"):
            hdr = st.columns(VGRID_W)
            for col, name in zip(hdr, ["Metric", "Expected", "Actual", "Variance", "Source"]):
                col.markdown(name)
        for i, row in enumerate(rows):
            b = (lambda t: f"**{t}**") if row["key"] else (lambda t: t)
            with st.container(key=f"{'vkey' if row['key'] else 'vrow'}_{f.id}_{i}"):
                cols = st.columns(VGRID_W, vertical_alignment="center")
                cols[0].markdown(b(row["metric"]))
                cols[1].markdown(b(row["expected"]))
                cols[2].markdown(b(row["actual"]))
                cols[3].markdown(b(row["var"]))
                with cols[4].container(horizontal=True, gap="small", vertical_alignment="center"):
                    for j, part in enumerate(row["source"]):
                        kind = part[0]
                        if kind == "text":
                            st.markdown(part[1], width="content")
                        elif kind == "doc" and part[1]:
                            if st.button(part[1], key=f"vd_{f.id}_{i}_{j}", type="tertiary"):
                                contract_dialog(part[1])
                        elif kind == "dl":
                            label, which, hl = part[1], part[2], part[3]
                            fn = {"inv": dl_invoice_lines, "usage": dl_usage, "gl": dl_gl}[which]
                            st.download_button(f"{label} ⬇", fn(cust, hl), key=f"vl_{f.id}_{i}_{j}", type="tertiary",
                                               file_name=f"{cust.name.split(',')[0].replace(' ', '_')}_{label.replace(' ', '_')}.xlsx")


def render_finding(f, key_prefix=""):
    with st.expander(f"{GROUP_ICON[f.classification]} **{f.id}** · {f.customer.replace(', Inc.', '')} · {f.title}  {_impact(f)}"):
        cust = next((c for c in ss.result.customers if c.ns_id == f.ns_id), None)
        if f.root_cause:
            st.markdown(f"**Root cause:** {_first_sentence(f.root_cause)}")
        rows = _variance_rows(f)
        if rows and cust:
            _render_variance_grid(f, rows, cust)
        else:
            facts = [("Detail", e) for e in f.evidence[:4]]
            if f.conditional_exposure:
                facts.append(("Exposure (not in totals)", f.conditional_exposure))
            if facts:
                _table(facts, ["Item", "Detail"])
        acts = []
        if f.billing_action:
            acts.append(("Billing Updates", f.billing_action))
        if f.classification == ERROR and f.accounting_action:
            acts.append(("Accounting Correction", f.accounting_action))
        if not acts and f.next_step:
            acts.append(("Next step", f.next_step))
        if f.owner:
            acts.append(("Owner", f.owner))
        if acts:
            _table(acts, ["Recommended Action", "Detail"])
        with st.container(key=f"custlink_{f.id}"):
            if cust and st.button(f"Open {cust.name.split(',')[0]} – {_month_name(ss.result.period_label)} monthly summary →",
                                  key=f"{key_prefix}cust_{f.id}", type="tertiary"):
                ss.detail_cust = cust.name
                ss[f"month_{cust.ns_id}"] = ss.result.period_label
                ss.detail_tab = "Monthly Summary"
                st.switch_page(PAGE_CUSTOMER)
        d_ = ss.decisions.get(f.id, {})
        cc = st.columns([1, 3])
        opts = ["Not reviewed", "Approve", "Reject", "Needs more info"]
        dec = cc[0].selectbox("Decision", opts, index=opts.index(d_.get("decision", "Not reviewed")), key=f"{key_prefix}d_{f.id}")
        note = cc[1].text_input("Note", value=d_.get("note", ""), key=f"{key_prefix}n_{f.id}")
        if dec != "Not reviewed":
            ss.decisions[f.id] = dict(decision=dec, reviewer=ss.reviewer, note=note)


GROUP_LABEL = {ERROR: "Errors", NEEDS_INFO: "Needs information", FOLLOW_UP: "Later-service follow-ups", VALID: "Valid explanations"}
GROUP_ICON = {ERROR: ":red[:material/error:]", NEEDS_INFO: ":orange[:material/help:]",
              FOLLOW_UP: ":blue[:material/schedule:]", VALID: ":green[:material/check_circle:]"}


def _impact(f):
    """Short headline amount: billed vs contract (actual − calculated)."""
    if f.billing_effect:
        v = -f.billing_effect
        return f"({fmt_usd(v, True)} {'over-billed' if v > 0 else 'under-billed'})"
    if f.revenue_effect:
        v = -f.revenue_effect
        return f"({fmt_usd(v, True)} revenue {'overstated' if v > 0 else 'understated'})"
    return ""


def _recon_table(title, calc_label, actual_label, calc, actual, under_label, under, over_label, over):
    var = actual - calc
    fmt = lambda v: f"({fmt_usd(-v)})" if v < 0 else fmt_usd(v)
    rows = [(calc_label, fmt(calc)), (actual_label, fmt(actual)), ("Variance", fmt(var)),
            (f"  {under_label}", fmt(-under)), (f"  {over_label}", fmt(over))]
    st.markdown(f"**{title}**")
    df = pd.DataFrame(rows, columns=["", "Amount"])
    st.dataframe(df, hide_index=True, width="stretch", height=38 + 35 * len(df),
                 column_config={"": st.column_config.TextColumn(width="medium"),
                                "Amount": st.column_config.TextColumn(width="small")})


def page_findings():
    st.title("Findings and Next Steps")
    if not need_result():
        return
    r = ss.result
    tab_f, tab_n = st.tabs(["Findings", "Next Steps"])
    with tab_n:
        render_recommendations(r)
    with tab_f:
        _findings_tab(r)


def _findings_tab(r):
    t = totals(r)
    left, right = st.columns(2)
    with left:
        _recon_table(f"Billing – {r.period_label} service", "Calculated billings", "Actual billings",
                     t["expected_platform"] + t["expected_usage"], t["actual_platform"] + t["billed_usage"],
                     "Under-billed", t["under_billed"], "Over-billed", t["over_billed"])
    with right:
        _recon_table(f"Revenue – {r.period_label}", "Calculated revenue", "Booked revenue (GL)",
                     t["expected_rev"] + t["expected_usage"], t["gl_rev"] + t["accrued_usage"],
                     "Understated", t["rev_understated"], "Overstated", t["rev_overstated"])
    show_changes()
    groups = [ERROR, NEEDS_INFO, FOLLOW_UP, VALID]
    counts = {g: len([f for f in r.findings if f.classification == g]) for g in groups}
    pick = st.pills("Show", groups, default=groups, selection_mode="multi", label_visibility="collapsed",
                    format_func=lambda g: f"{GROUP_ICON[g]} {GROUP_LABEL[g]} ({counts[g]})")
    for g in groups:
        items = [f for f in r.findings if f.classification == g and g in (pick or [])]
        if items:
            _section(f"{GROUP_ICON[g]} {GROUP_LABEL[g]} ({len(items)})")
            for f in items:
                render_finding(f)
    dd = decisions_df()
    if len(dd):
        st.download_button("⬇️ Save decisions", dd.to_csv(index=False), "reviewer_decisions.csv")


def render_recommendations(r):
    t = totals(r)
    c = st.columns(3)
    c[0].metric("Proposed JEs", len(r.journals))
    c[1].metric("Billing documents", len(r.billing_docs))
    c[2].metric("Net revenue effect", fmt_usd(t["rev_net"], True))
    st.caption("Proposed for Controller approval – not posted.")
    _section("Proposed Journal Entries")
    if not r.journals:
        st.success("No adjustments required.")
    for j in r.journals:
        st.markdown(f"**{j['je_id']}** · {j['customer']} · {j['title'].split(' – ')[0]} "
                    f"{'✅' if j['balanced'] else '❌ not balanced'}")
        df = pd.DataFrame([{"Account": f"{l['account']} {l['account_name']}", "Debit": f"{l['debit']:,.2f}" if l["debit"] else "",
                            "Credit": f"{l['credit']:,.2f}" if l["credit"] else "", "Memo": l["memo"]} for l in j["lines"]])
        st.dataframe(df, hide_index=True, width="stretch",
                     column_config={"Debit": st.column_config.TextColumn(width="small"), "Credit": st.column_config.TextColumn(width="small")})
        st.caption(j["calculation"][-1] if j["calculation"] else "")
    _section("Proposed Billing")
    bd = billing_docs_table(r)
    if bd.empty:
        st.success("No billing corrections.")
    else:
        show_df(bd.drop(columns=["Status"]))
    _section("Billing vs. Revenue")
    rows = [{"Finding": f.id, "Customer": f.customer, "Billing effect": float(f.billing_effect),
             "Oct revenue effect": float(f.revenue_effect), "Why": f.why_billing_only}
            for f in r.findings if f.classification == ERROR]
    show_df(pd.DataFrame(rows))
    _section("Revenue Plan Updates")
    if r.plan_updates:
        st.dataframe(pd.DataFrame(r.plan_updates)[["customer", "source_document", "recommendation"]]
                     .rename(columns=str.capitalize).rename(columns={"Source_document": "Document"}),
                     hide_index=True, width="stretch")
    else:
        st.success("No revenue plan updates.")


def page_close():
    st.title("Recommended Actions")
    if not need_result():
        return
    r = ss.result
    tabs = st.tabs(["Balance roll-forward", "Next-month checklist"])
    with tabs[1]:
        st.markdown(NEXT_MONTH_CHECKLIST)
    with tabs[0]:
        rf = rollforward_table(r)
        rf = rf[rf["Account"].str[:4].isin(["1100", "1150", "1160", "2200"])]
        if st.toggle("Adjusted customers only", value=True):
            adj = set(rf.loc[rf["Proposed adjustments"] != 0, "Customer"])
            res = set(rf.loc[(rf["Account"].str.startswith("1150")) & (rf["Balance after next-month activity"].abs() > 0.004), "Customer"])
            rf = rf[rf["Customer"].isin(adj | res)]
        show_df(rf, height=38 + 35 * max(len(rf), 1))


CONTROL_SHORT = {
    "Contract terms on file": "Terms", "Amendments reflected in billing": "Amend.",
    "Billed rate matches contract rate": "Rate", "Billed quantity matches calculated excess": "Qty",
    "Usage invoice completeness": "Invoiced", "Usage accrual equals recalculated usage": "Accrual",
    "Late-ingested usage": "Late", "Platform invoices match contract schedule": "Plat. bill",
    "Platform recognition = contract days/365": "Plat. rev", "Revenue plan includes all amendments": "Plan",
    "Contract asset reclass correct": "Asset", "Usage mapped to verified customer": "Mapped"}


NEXT_MONTH_CHECKLIST = """
1. Load every executed amendment to billing before the 1st.
2. Compare Stripe usage prices to contract rates; flag any change.
3. Hold usage invoicing until the final daily batch lands.
4. Run this review on draft invoices, fix exceptions, then finalize.
5. Build the usage accrual from this review, not from billed amounts.
6. Every customer with overage has an accrual and an invoice.
7. Add platform upgrades to the revenue plan when signed.
8. Product Ops clears unverified workspaces weekly.
9. Confirm approved corrections appear in Stripe exactly once.
"""


def render_controls(r):
    df = controls_table(r)
    body = df.drop(columns=["Customer"])
    c = st.columns(3)
    c[0].metric("Controls run", len(body.columns))
    c[1].metric("Checks failed", int((body == "❌ Fail").sum().sum()))
    c[2].metric("Customers affected", int((body == "❌ Fail").any(axis=1).sum()))
    if st.toggle("Failures only", value=True, key="controls_failures_only"):
        df = df[(body == "❌ Fail").any(axis=1) | (body == "🟠 Corrected").any(axis=1)]
    df = df.copy()
    df["Customer"] = df["Customer"].str.replace(r",? Inc\.?$", "", regex=True)
    df = df.rename(columns=CONTROL_SHORT).replace({"✅ Pass": "✅", "❌ Fail": "❌", "🟠 Corrected": "🟠", "— n/a": "–"})
    st.dataframe(df, hide_index=True, width="stretch", height=38 + 35 * max(len(df), 1),
                 column_config={c_: st.column_config.TextColumn(help=full) for full, c_ in CONTROL_SHORT.items()})
    with st.expander("What each control checks"):
        cd = control_definitions()
        cd.insert(0, "Column", cd["Control"].map(CONTROL_SHORT))
        st.dataframe(cd, hide_index=True, width="stretch")


def page_assumptions():
    st.title("Assumptions & Open Items")
    if not need_result():
        return
    r = ss.result
    st.subheader("Open items")
    rows = [{"Finding": f.id, "Customer": f.customer.replace(", Inc.", ""), "Item": f.title, "Exposure (not in totals)": f.conditional_exposure or "–",
             "Next step": f.next_step, "Owner": f.owner}
            for f in r.findings if f.classification in (NEEDS_INFO, FOLLOW_UP)]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.subheader("Judgment calls")
    st.dataframe(pd.DataFrame([
        ("Unverified workspace", "Not assigned to any customer; exposure shown as a range and excluded from totals (MSG-2026-131/132)."),
        ("Platform billing basis", "Invoices covering the month; revenue measured separately at days/365."),
        ("Meridian catch-up", "Recognized in October as a separate increment; held in 1160 until billed in November."),
        ("Concessions", "Only Controller-approved concessions reduce consideration, and only once."),
        ("Late usage", "Accrual is correct; fix is a supplemental invoice, not a revenue entry."),
    ], columns=["Topic", "Treatment"]), hide_index=True, width="stretch")
    st.subheader("Approvals used")
    if len(ss.approvals):
        ap = ss.approvals
        st.dataframe(pd.DataFrame({"Ref": ap["reference"], "Date": ap["date"], "Customer": ap["customer_name"],
                                   "Type": ap["type"], "Amount": ap["amount"], "Status": ap["status"]}),
                     hide_index=True, width="stretch")
    st.subheader("Accounting basis")
    st.dataframe(assumptions_table(), hide_index=True, width="stretch")


# ======================================================================= navigation
if "baseline" not in ss:  # every session starts from the prior close's carried-forward state
    apply_baseline(load_baseline())
PAGE_START = st.Page(page_start, title="File Upload", icon="📥")
PAGE_CONTRACTS = st.Page(page_contracts, title="Contracts & Terms", icon="📄", default=True)
PAGE_FINDINGS = st.Page(page_findings, title="Findings and Next Steps", icon="🚩")
PAGE_CUSTOMER = st.Page(page_customer, title="Customer Detail", icon="🔎")
pg = st.navigation({
    "Customers & Contracts": [PAGE_CONTRACTS,
                              PAGE_CUSTOMER],
    "Close Review": [PAGE_START,
                     PAGE_FINDINGS,
                     st.Page(page_health, title="Data Quality", icon="🩺"),
                     st.Page(page_summary, title="Customer Summary", icon="📊"),
                     st.Page(page_close, title="Recommended Actions", icon="📒"),
                     st.Page(page_assumptions, title="Assumptions & Open Items", icon="📝")],
})
pg.run()
sidebar()  # after the page, so totals reflect a review that the page just ran
