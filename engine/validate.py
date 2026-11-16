"""Data health: completeness checks and source reconciliations run before any math.

Each check returns a row with a plain-English name, result (Pass / Fail / Warning / Info),
what was found, and what to do about it.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Dict, List

import pandas as pd

from .sources import SOURCE_BY_KEY
from .util import D, cents_to_usd, fmt_int, fmt_usd, is_blank, parse_date, parse_ts

PASS, FAIL, WARN, INFO = "Pass", "Fail", "Warning", "Info"


def _chk(area, name, result, detail, action="", blocking=False):
    return dict(area=area, check=name, result=result, detail=detail, action=action, blocking=blocking)


def prepare_usage(usage: pd.DataFrame) -> dict:
    """Deduplicate on record_id (the source key) and drop superseded / non-active versions."""
    raw = usage.copy()
    raw["_row"] = range(len(raw))
    cols = [c for c in raw.columns if c != "_row"]
    exact_dupes = raw[raw.duplicated(subset=cols, keep="first")]
    by_id = raw.drop_duplicates(subset=cols, keep="first")
    conflicting = by_id[by_id.duplicated(subset=["record_id"], keep=False)]
    replaced_ids = set(by_id.loc[by_id["replaces_record_id"].astype(str).str.strip() != "", "replaces_record_id"])
    clean = by_id.drop_duplicates(subset=["record_id"], keep="last")
    superseded = clean[clean["record_id"].isin(replaced_ids)]
    clean = clean[~clean["record_id"].isin(replaced_ids)]
    inactive = clean[clean["record_status"].str.strip().str.lower() != "active"]
    clean = clean[clean["record_status"].str.strip().str.lower() == "active"].copy()
    clean["service_date_d"] = clean["service_date"].map(parse_date)
    clean["units_i"] = clean["units"].map(lambda u: int(D(u)))
    clean["ingested_dt"] = clean["ingested_at"].map(parse_ts)
    return dict(raw=raw, clean=clean, exact_dupes=exact_dupes, conflicting=conflicting,
                superseded=superseded, inactive=inactive)


def run_checks(t: Dict[str, pd.DataFrame], usage_prep: dict, expected_counts: Dict[str, int],
               cutoff: datetime, contracts_problems: List[str], contracts: dict) -> List[dict]:
    out: List[dict] = []

    # ---------------------------------------------------- 1. row counts
    if not expected_counts:
        out.append(_chk("Completeness", "Files loaded", INFO,
                        f"{sum(1 for k in t if k in SOURCE_BY_KEY)} files · {fmt_int(sum(len(v) for k, v in t.items() if k in SOURCE_BY_KEY))} rows"))
    for key, n_exp in expected_counts.items():
        if key == "usage_unique_records" or key not in t:
            continue
        n = len(t[key])
        fname = SOURCE_BY_KEY[key].filename
        if n_exp in (None, ""):
            out.append(_chk("Completeness", f"Row count – {fname}", INFO, f"{fmt_int(n)} rows"))
        elif int(n_exp) == n:
            out.append(_chk("Completeness", f"Row count – {fname}", PASS, f"{fmt_int(n)} rows, as expected"))
        else:
            out.append(_chk("Completeness", f"Row count – {fname}", FAIL,
                            f"{fmt_int(n)} rows vs {fmt_int(n_exp)} expected",
                            "Confirm the export filter/date range with the data owner before relying on results."))

    # ---------------------------------------------------- 2. usage dedup
    raw, clean = usage_prep["raw"], usage_prep["clean"]
    nd = len(usage_prep["exact_dupes"])
    out.append(_chk("Usage", "Duplicate usage rows", INFO if nd else PASS,
                    (f"{nd} removed ({', '.join(sorted(set(usage_prep['exact_dupes']['record_id'])))}) · "
                     f"{fmt_int(len(clean))} unique records") if nd else f"None · {fmt_int(len(clean))} unique records",
                    "Duplicates are not new activity and are never billed." if nd else ""))
    nconf = usage_prep["conflicting"]["record_id"].nunique()
    if nconf:
        out.append(_chk("Usage", "Conflicting record versions", FAIL,
                        f"{nconf} record_id(s) with different values",
                        "Ask the metering team which version is authoritative.", blocking=True))
    exp_u = expected_counts.get("usage_unique_records")
    if exp_u not in (None, ""):
        ok = int(exp_u) == len(clean) + len(usage_prep["superseded"]) + len(usage_prep["inactive"])
        out.append(_chk("Completeness", "Unique usage records", PASS if ok else FAIL,
                        f"{fmt_int(len(clean))} vs {fmt_int(exp_u)} expected",
                        "" if ok else "Confirm the usage export covers every service date."))

    # ---------------------------------------------------- 3. batch controls
    ctl = t["batch_controls"]
    agg = clean.groupby("batch_id").agg(n=("record_id", "nunique"), u=("units_i", "sum"))
    raw_agg = raw.assign(u=raw["units"].map(lambda u: int(D(u)))).groupby("batch_id").agg(n=("record_id", "size"), u=("u", "sum"))
    bad, raw_bad = [], []
    for r in ctl.to_dict("records"):
        b = r["batch_id"]
        n, u = (int(agg.loc[b, "n"]), int(agg.loc[b, "u"])) if b in agg.index else (0, 0)
        if n != int(D(r["distinct_active_record_count"])) or u != int(D(r["active_units_total"])):
            bad.append(f"{b}: file {n} records / {fmt_int(u)} runs vs control {r['distinct_active_record_count']} / {fmt_int(D(r['active_units_total']))}")
        if b in raw_agg.index and (int(raw_agg.loc[b, "n"]) != int(D(r["distinct_active_record_count"])) or int(raw_agg.loc[b, "u"]) != int(D(r["active_units_total"]))):
            raw_bad.append(f"{b} (raw {int(raw_agg.loc[b,'n'])} records / {fmt_int(raw_agg.loc[b,'u'])} runs)")
    missing_ctl = sorted(set(clean["batch_id"]) - set(ctl["batch_id"]))
    out.append(_chk("Usage", "Batch control tie-out",
                    PASS if not bad and not missing_ctl else FAIL,
                    (f"All {len(ctl)} daily batches tie"
                     if not bad and not missing_ctl else "; ".join(bad + [f"{b}: no control row" for b in missing_ctl])),
                    "" if not bad else "Usage file is incomplete or contains extra records – do not bill from it until resolved.",
                    blocking=bool(bad)))
    if raw_bad:
        out.append(_chk("Usage", "Batch tied after de-duplication", INFO, ", ".join(x.split(" ")[0] for x in raw_bad),
                        "Explained by the duplicate rows above."))

    # ---------------------------------------------------- 4. mapping coverage
    mp = t["mapping"]
    known_pa = set(mp["product_account_id"])
    orphan = sorted(set(clean["product_account_id"]) - known_pa)
    out.append(_chk("Mapping", "Usage workspaces registered",
                    PASS if not orphan else FAIL,
                    "All registered" if not orphan else f"Unregistered: {', '.join(orphan)}",
                    "" if not orphan else "Product Operations to register and verify owner."))
    unv = mp[mp["salesforce_account_id"].str.strip() == ""]
    for r in unv.to_dict("records"):
        u = int(clean.loc[clean["product_account_id"] == r["product_account_id"], "units_i"].sum())
        out.append(_chk("Mapping", "Workspace without verified owner", WARN,
                        f"{r['product_account_id']} · {fmt_int(u)} runs unassigned",
                        "Isolated as unverified usage. Product Operations must verify ownership before Billing assigns it."))
    acc = t["accounts"]
    mapped_sf = set(mp["salesforce_account_id"].str.strip())
    for r in acc.to_dict("records"):
        if r["AccountId"] not in mapped_sf:
            out.append(_chk("Mapping", "Customer without workspace", WARN,
                            f"{r['AccountName']} · no product account",
                            "Product Operations to confirm whether the customer has a workspace."))

    # ---------------------------------------------------- 5. cross-system IDs
    stripe_ids = set(acc["StripeCustomerId__c"])
    ns_ids = set(acc["NetSuiteCustomerInternalId__c"])
    bad_inv = sorted(set(t["invoices"]["customer_id"]) - stripe_ids)
    bad_gl = sorted(set(t["gl"]["Customer Internal ID"]) - ns_ids)
    ok = not (bad_inv or bad_gl)
    out.append(_chk("Mapping", "Cross-system IDs",
                    PASS if ok else FAIL,
                    "Stripe and NetSuite customers all link to Salesforce"
                    if ok else f"Unlinked – Stripe: {bad_inv}; NetSuite: {bad_gl}",
                    "" if ok else "Fix the cross-system ID on the Salesforce account."))

    # ---------------------------------------------------- 6. Stripe internal tie
    lines = t["invoice_lines"]
    line_sum = lines.groupby("invoice_id")["amount"].apply(lambda s: sum(int(D(x)) for x in s))
    disc_sum = lines.groupby("invoice_id")["discount_amount"].apply(lambda s: sum(int(D(x)) for x in s))
    mism = []
    for r in t["invoices"].to_dict("records"):
        sub = int(D(r["subtotal"]))
        if line_sum.get(r["id"], 0) != sub or sub - int(D(r["total_discount_amount"])) != int(D(r["total"])):
            mism.append(r["number"])
    out.append(_chk("Billing data", "Invoice totals = lines", PASS if not mism else FAIL,
                    f"All {len(t['invoices'])} invoices tie" if not mism else f"Do not tie: {', '.join(mism)}"))
    void = t["credit_notes"][t["credit_notes"]["status"].str.lower() != "issued"]
    if len(void):
        out.append(_chk("Billing data", "Void credit notes excluded", INFO,
                        ", ".join(f"{r['number']} ({fmt_usd(cents_to_usd(r['amount']))})" for r in void.to_dict("records")),
                        "Void notes have no credit effect."))

    # ---------------------------------------------------- 7. cutoff
    late = []
    for key, col, label in [("invoices", "created", "invoices"), ("credit_notes", "created", "credit notes"),
                            ("gl", "Created At", "GL lines")]:
        n = sum(1 for v in t[key][col] if not is_blank(v) and parse_ts(v) > cutoff)
        if n:
            late.append(f"{n} {label}")
    n_u = int((clean["ingested_dt"] > cutoff).sum())
    if n_u:
        late.append(f"{n_u} usage records")
    out.append(_chk("Cutoff", "Records after cutoff", INFO if late else PASS,
                    ("Excluded: " + ", ".join(late)) if late else "None"))

    # ---------------------------------------------------- 8. GL integrity
    gl = t["gl"]
    je = gl.assign(dr=gl["Debit"].map(D), cr=gl["Credit"].map(D)).groupby("Internal ID")[["dr", "cr"]].sum()
    unbal = je[je["dr"] != je["cr"]]
    out.append(_chk("Accounting data", "Journals balance", PASS if unbal.empty else FAIL,
                    f"All {len(je)} journals balance" if unbal.empty else f"Unbalanced: {', '.join(unbal.index)}",
                    blocking=not unbal.empty))
    inv_by_id = {r["id"]: r for r in t["invoices"].to_dict("records")}
    cn_by_id = {r["id"]: r for r in t["credit_notes"].to_dict("records")}
    ext_bad = []
    for ext, grp in gl[gl["External Source ID"].str.strip() != ""].groupby("External Source ID"):
        ar = sum(D(x) for x in grp.loc[grp["Account Number"] == "1100", "Debit"]) - sum(D(x) for x in grp.loc[grp["Account Number"] == "1100", "Credit"])
        if ext in inv_by_id:
            if abs(ar) != cents_to_usd(inv_by_id[ext]["total"]):
                ext_bad.append(f"{ext} amount")
        elif ext in cn_by_id:
            if abs(ar) != cents_to_usd(cn_by_id[ext]["amount"]):
                ext_bad.append(f"{ext} amount")
        else:
            ext_bad.append(f"{ext} not in Stripe")
    n_ext = gl["External Source ID"].str.strip().replace("", pd.NA).dropna().nunique()
    out.append(_chk("Accounting data", "GL postings match Stripe", PASS if not ext_bad else FAIL,
                    f"All {n_ext} documents match" if not ext_bad else "; ".join(ext_bad)))
    plan_ids = set(t["rev_plan"]["Revenue Plan ID"])
    bad_plan = sorted(set(gl.loc[gl["Revenue Plan ID"].str.strip() != "", "Revenue Plan ID"]) - plan_ids)
    out.append(_chk("Accounting data", "Recognition linked to revenue plan", PASS if not bad_plan else FAIL,
                    "All linked" if not bad_plan else f"Unknown plans: {bad_plan}"))
    ob = t["opening"]
    need = {(c, a) for c in ns_ids for a in ("1100", "1150", "1160", "2200")}
    have = set(zip(ob["Customer Internal ID"], ob["Account Number"]))
    miss = need - have
    out.append(_chk("Accounting data", "Opening balances complete", PASS if not miss else FAIL,
                    f"{len(have & need)} of {len(need)} present"))

    # ---------------------------------------------------- 9. contract repository
    for p in contracts_problems:
        out.append(_chk("Contracts", "Contract document not linked", WARN, p, "Check the document on Contracts & Terms."))
    no_contract = [r["AccountName"] for r in acc.to_dict("records") if r["AccountId"] not in contracts]
    out.append(_chk("Contracts", "Every customer has a contract", PASS if not no_contract else WARN,
                    f"{len(contracts)} customers · {sum(1 + len(c.amendments) for c in contracts.values())} documents"
                    if not no_contract else f"No order form: {', '.join(no_contract)}"))
    return out
