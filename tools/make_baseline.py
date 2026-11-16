"""Build data/baseline_sep_2026/ – the state carried forward from the September 2026 close.

It holds what the team already had on file before October: customer accounts, product-account mapping,
the 30 executed order forms (PDFs) and the terms extracted from them.
The two October amendments are NOT in the baseline – they are the new contract documents for October.
Run:  python tools/make_baseline.py
"""
import glob
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import pandas as pd  # noqa: E402

from engine.extract_terms import TERMS_COLUMNS, extract_terms  # noqa: E402

SRC = os.path.join(ROOT, "data", "sample_oct_2026")
DST = os.path.join(ROOT, "data", "baseline_sep_2026")


def main():
    if os.path.exists(DST):
        shutil.rmtree(DST)
    os.makedirs(os.path.join(DST, "contracts"))
    for f in ("salesforce_accounts.csv", "product_account_mapping.csv"):
        shutil.copy(os.path.join(SRC, f), DST)
    rows = []
    for pdf in sorted(glob.glob(os.path.join(SRC, "contracts", "OF-*.pdf"))):
        shutil.copy(pdf, os.path.join(DST, "contracts"))
        ex = extract_terms(open(pdf, "rb").read())
        ex["term_source"] = "Carried forward – Sep 2026 close"
        rows.append(ex)
    terms = pd.DataFrame(rows)[TERMS_COLUMNS]
    reg = pd.read_csv(os.path.join(SRC, "contract_register.csv"), dtype=str, keep_default_na=False)
    loaded = dict(zip(reg["document_id"], reg["billing_loaded_at"]))
    terms["billing_loaded_at"] = terms["document_id"].map(loaded).fillna("")
    terms.to_csv(os.path.join(DST, "contract_terms.csv"), index=False)

    # September close snapshot (the "before" view for October), rebuilt from the supplied files.
    from engine.packet import load_folder
    from engine.review import run_review
    from engine.snapshot import build_snapshot, september_overrides
    from engine.util import parse_ts
    tables = load_folder(SRC)
    sep = run_review(tables, terms.fillna(""), pd.DataFrame(), 2026, 9, parse_ts("2026-10-12T18:00:00-06:00"), {})
    accrued, revenue, balances = september_overrides(tables)
    snap = build_snapshot(sep, accrued, revenue, balances)
    snap["customers"].to_csv(os.path.join(DST, "snapshot_customers.csv"), index=False)
    snap["invoices"].to_csv(os.path.join(DST, "snapshot_invoices.csv"), index=False)
    c = snap["customers"]
    assert (abs(c["usage_expected"] - c["usage_billed"]) < 0.005).all() and (abs(c["usage_expected"] - c["usage_accrued"]) < 0.005).all()
    open(os.path.join(DST, "LABEL.txt"), "w").write("Sep 2026 close\n")
    print(f"written {DST}: {len(rows)} order forms")


if __name__ == "__main__":
    main()
