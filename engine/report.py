"""Tables for the UI and the Excel workbook. Every amount is a float for display only;
calculations stay in Decimal inside the engine."""
from __future__ import annotations

import io
from datetime import timedelta
from decimal import Decimal
from typing import Dict, List

import pandas as pd

from .review import ERROR, FOLLOW_UP, NEEDS_INFO, VALID, CONTROL_DEFS, ReviewResult
from .util import ZERO, fmt_usd


def f(x) -> float:
    return float(x) if x is not None else None


def customer_summary(r: ReviewResult) -> pd.DataFrame:
    rows = []
    for c in r.customers:
        u = r.usage.get(c.ns_id, {})
        p = r.platform.get(c.ns_id, {})
        k = r.contracts.get(c.sf_id)
        fs = [x for x in r.findings if x.ns_id == c.ns_id]
        errs = [x for x in fs if x.classification == ERROR]
        status = ("❌ Error" if errs else "❓ Needs information" if any(x.classification == NEEDS_INFO for x in fs)
                  else "ℹ️ Explained" if fs else "✅ Ties")
        rows.append({
            "Customer": c.name,
            "Status": status,
            "Platform billing": (k.billing_frequency if k else ""),
            "Platform: expected invoices covering month": f(p.get("expected_cover", ZERO)),
            "Platform: actual invoices covering month": f(p.get("actual_cover", ZERO)),
            "Platform billing difference": f(p.get("expected_cover", ZERO) - p.get("actual_cover", ZERO)),
            "Platform revenue: expected (days/365)": f(p.get("expected_rev", ZERO)),
            "Platform revenue: GL 4000": f(p.get("gl_rev", ZERO)),
            "Platform revenue difference": f(p.get("expected_rev", ZERO) - p.get("gl_rev", ZERO)),
            "Usage runs": u.get("units", 0),
            "Allowance": u.get("allowance", 0),
            "Excess runs": u.get("excess", 0),
            "Rate": f(u.get("rate", ZERO)),
            "Usage: expected (net)": f(u.get("net", ZERO)),
            "Usage: billed (net of credits)": f(u.get("billed_net", ZERO)),
            "Usage billing difference": f(u.get("net", ZERO) - u.get("billed_net", ZERO)),
            "Usage: accrued GL 4010": f(u.get("accrued", ZERO)),
            "Usage revenue difference": f(u.get("net", ZERO) - u.get("accrued", ZERO)),
            "Findings": len(fs),
        })
    return pd.DataFrame(rows)


def totals(r: ReviewResult) -> Dict[str, Decimal]:
    errs = [x for x in r.findings if x.classification == ERROR]
    return dict(
        under_billed=sum((x.billing_effect for x in errs if x.billing_effect > 0), ZERO),
        over_billed=-sum((x.billing_effect for x in errs if x.billing_effect < 0), ZERO),
        rev_understated=sum((x.revenue_effect for x in errs if x.revenue_effect > 0), ZERO),
        rev_overstated=-sum((x.revenue_effect for x in errs if x.revenue_effect < 0), ZERO),
        rev_net=sum((x.revenue_effect for x in errs), ZERO),
        n_errors=len(errs),
        n_needs_info=len([x for x in r.findings if x.classification == NEEDS_INFO]),
        n_valid=len([x for x in r.findings if x.classification == VALID]),
        n_follow=len([x for x in r.findings if x.classification == FOLLOW_UP]),
        expected_platform=sum((p["expected_cover"] for p in r.platform.values()), ZERO),
        actual_platform=sum((p["actual_cover"] for p in r.platform.values()), ZERO),
        expected_usage=sum((u["net"] for u in r.usage.values()), ZERO),
        billed_usage=sum((u["billed_net"] for u in r.usage.values()), ZERO),
        accrued_usage=sum((u["accrued"] for u in r.usage.values()), ZERO),
        expected_rev=sum((p["expected_rev"] for p in r.platform.values()), ZERO),
        gl_rev=sum((p["gl_rev"] for p in r.platform.values()), ZERO),
    )


def findings_table(r: ReviewResult) -> pd.DataFrame:
    return pd.DataFrame([x.as_row() for x in r.findings])


def journals_table(r: ReviewResult) -> pd.DataFrame:
    rows = []
    for j in r.journals:
        for i, l in enumerate(j["lines"], 1):
            rows.append({"Proposed JE": j["je_id"], "Finding": j["finding"], "Date": str(j["date"]),
                         "Posting period": j["posting_period"], "Customer": j["customer"], "Line": i,
                         "Account": l["account"], "Account name": l["account_name"],
                         "Debit": f(l["debit"]) if l["debit"] else None, "Credit": f(l["credit"]) if l["credit"] else None,
                         "Memo": l["memo"], "Calculation": "\n".join(j["calculation"]) if i == 1 else "",
                         "Status": "Proposed – pending Controller approval"})
    return pd.DataFrame(rows)


def billing_docs_table(r: ReviewResult) -> pd.DataFrame:
    return pd.DataFrame([{"Finding": b["finding"], "Customer": b["customer"], "Document": b["type"],
                          "Linked to": b["linked_to"], "Service period": b["service_period"], "Amount": f(b["amount"]),
                          "Issue date": b["issue_date"], "GL when issued": b["gl_when_issued"],
                          "Status": "Proposed – pending Controller approval"} for b in r.billing_docs])


def rollforward_table(r: ReviewResult) -> pd.DataFrame:
    nl = r.period_label
    return pd.DataFrame([{"Customer": x["customer"], "Account": f"{x['account']} {x['account_name']}",
                          "Opening (Dr+/Cr-)": f(x["opening"]), f"{nl} GL activity": f(x["activity"]),
                          f"{nl} close per GL": f(x["gl_close"]), "Proposed adjustments": f(x["proposed"]),
                          f"Corrected {nl} close": f(x["corrected_close"]),
                          "Next-month activity through cutoff": f(x["next_activity"]),
                          "Balance after next-month activity": f(x["after_next"]),
                          "Tie-out": ("✅ = expected usage" if x.get("ties") else "❌ ≠ expected usage") if "ties" in x else ""}
                         for x in r.rollforward])


def controls_table(r: ReviewResult) -> pd.DataFrame:
    return pd.DataFrame(r.controls)


def control_definitions() -> pd.DataFrame:
    return pd.DataFrame([{"Control": n, "What it checks": d} for n, d in CONTROL_DEFS])


def checks_table(r: ReviewResult) -> pd.DataFrame:
    return pd.DataFrame([{"Area": c["area"], "Check": c["check"], "Result": c["result"], "What we found": c["detail"],
                          "What to do": c["action"]} for c in r.checks])


ASSUMPTIONS = [
    ("Source precedence", "Executed order forms and amendments in the contract repository set commercial terms; an amendment changes only the terms it names. Whether an amendment reached billing is judged from the Stripe invoices."),
    ("Usage", "De-duplicate on record_id; aggregate all verified product accounts per customer; one calendar-month allowance; no rollover or tiers. Explicit zero-use days stay as records."),
    ("Allowance", "First partial month and mid-month amendments: allowance × applicable days / days in month, summed and rounded once to whole runs, half up."),
    ("Charge", "max(0, runs − allowance) × contract rate, rounded once to cents, half up. Prices are net – no invoice discounts."),
    ("Platform billing basis", "Reconciled by invoice billing periods that cover the month (annual prepaid invoices cover the month without a new invoice). A day-based allocation of each invoice to the month is shown for reference only."),
    ("Platform revenue", "Annual consideration × actual service days / 365, independent of invoicing. Each layer (base + upgrade increment) rounded once per month; the final period absorbs rounding; the base schedule is preserved."),
    ("Contract asset", "Any customer net debit in 2200 is reclassified to 1160. Prior reclass reversed before recalculating; only the incremental change is proposed."),
    ("Usage accounting", "Dr 1150 / Cr 4010 in the service month. Next-month invoices are Dr 1100 / Cr 1150 (not revenue again). Residuals stay in 1150 until a supported billing correction is issued."),
    ("Credits", "Issued credit notes only; void notes have no effect. Approved concessions reduce the service month's consideration once; unapproved requests have no effect."),
    ("Corrections", "Proposed billing documents use November issue dates; finalized invoices are never edited. October-service accounting corrections are posted in the October period. Nothing is created in live systems."),
    ("Cutoff", "Only records created/ingested on or before the close cutoff are used."),
    ("Unverified usage", "Never assigned by elimination or name matching. Reported as conditional exposure (range across all customer contracts) and excluded from confirmed totals."),
]


def assumptions_table() -> pd.DataFrame:
    return pd.DataFrame(ASSUMPTIONS, columns=["Topic", "Basis used"])


def to_excel(r: ReviewResult, terms: pd.DataFrame, approvals: pd.DataFrame, decisions: pd.DataFrame = None) -> bytes:
    t = totals(r)
    summary = pd.DataFrame([
        ("Review period", r.period_label),
        ("Close cutoff", str(r.cutoff)),
        ("Under-billed (confirmed errors)", f(t["under_billed"])),
        ("Over-billed (confirmed errors)", f(t["over_billed"])),
        ("October revenue understated", f(t["rev_understated"])),
        ("October revenue overstated", f(t["rev_overstated"])),
        ("Net October revenue effect", f(t["rev_net"])),
        ("Errors", t["n_errors"]), ("Needs information", t["n_needs_info"]),
        ("Valid explanations", t["n_valid"]), ("Later-service follow-ups", t["n_follow"]),
        ("Note", "Conditional exposure (unverified usage, unapproved requests) is listed per finding and never included above."),
    ], columns=["Item", "Value"])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        sheets = [("Summary", summary), ("Customer reconciliation", customer_summary(r)), ("Findings", findings_table(r)),
                  ("Proposed JEs", journals_table(r)), ("Proposed billing docs", billing_docs_table(r)),
                  ("Revenue plan updates", pd.DataFrame(r.plan_updates)), ("Balance roll-forward", rollforward_table(r)),
                  ("Controls matrix", controls_table(r)), ("Control definitions", control_definitions()),
                  ("Data health", checks_table(r)), ("Contract terms", terms), ("Approvals log", approvals),
                  ("Assumptions", assumptions_table())]
        if decisions is not None and len(decisions):
            sheets.insert(3, ("Reviewer decisions", decisions))
        for name, df in sheets:
            df.to_excel(xw, sheet_name=name[:31], index=False)
            ws = xw.sheets[name[:31]]
            ws.freeze_panes = "A2"
            for i, col in enumerate(df.columns, 1):
                width = min(60, max(10, len(str(col)) + 2, *(len(str(v)) for v in df[col].head(50).astype(str).map(lambda s: s.split("\n")[0]))))
                ws.column_dimensions[ws.cell(1, i).column_letter].width = width
                if df[col].dtype.kind == "f":
                    for cell in ws.iter_cols(min_col=i, max_col=i, min_row=2):
                        for c in cell:
                            c.number_format = '#,##0.00;(#,##0.00);"-"'
    return buf.getvalue()
