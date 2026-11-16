"""Per-customer close snapshot carried into the next month (the "before" view).

A snapshot holds, for each customer, what the month closed at: expected vs billed platform and usage,
usage accrued, platform revenue recognised, and month-end balances – plus the invoices behind them.
The September 2026 snapshot is rebuilt from the supplied files (September balances are final); every
later month's snapshot is written from that month's review when it is saved as the next starting point.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, Optional

import pandas as pd

from .review import ReviewResult
from .util import ZERO, D, cents_to_usd, money, parse_period

SNAPSHOT_COLUMNS = ["customer", "ns_id", "period", "billing", "platform_expected", "platform_billed", "platform_revenue",
                    "usage_runs", "allowance", "excess", "rate", "usage_expected", "usage_billed", "usage_accrued",
                    "bal_1100", "bal_1150", "bal_1160", "bal_2200", "usage_calc", "platform_calc"]


def _f(x) -> float:
    return float(x) if x is not None else 0.0


def build_snapshot(r: ReviewResult, accrued: Optional[Dict[str, Decimal]] = None,
                   revenue: Optional[Dict[str, Decimal]] = None,
                   balances: Optional[Dict[str, Dict[str, Decimal]]] = None) -> Dict[str, pd.DataFrame]:
    """Snapshot from a review. Overrides supply booked figures when they come from outside the review."""
    rows, inv = [], []
    rf = {(x["ns_id"], x["account"]): x for x in r.rollforward}
    for c in r.customers:
        u, p, k = r.usage.get(c.ns_id, {}), r.platform.get(c.ns_id, {}), r.contracts.get(c.sf_id)
        bal = (balances or {}).get(c.ns_id) or {a: rf[(c.ns_id, a)]["corrected_close"] for a in ("1100", "1150", "1160", "2200")
                                                if (c.ns_id, a) in rf}
        rows.append(dict(
            customer=c.name, ns_id=c.ns_id, period=r.period_label, billing=k.billing_frequency if k else "",
            platform_expected=_f(p.get("expected_cover")), platform_billed=_f(p.get("actual_cover")),
            platform_revenue=_f((revenue or {}).get(c.ns_id, p.get("expected_rev"))),
            usage_runs=u.get("units", 0), allowance=u.get("allowance", 0), excess=u.get("excess", 0), rate=_f(u.get("rate")),
            usage_expected=_f(u.get("net")), usage_billed=_f(u.get("billed_net")),
            usage_accrued=_f((accrued or {}).get(c.ns_id, u.get("net"))),
            **{f"bal_{a}": _f(bal.get(a, ZERO)) for a in ("1100", "1150", "1160", "2200")},
            usage_calc=" | ".join(u.get("calc", [])),
            platform_calc=" | ".join(f"{x['layer']}: {x['calc']}" for x in p.get("recognition", []))))
        bl = u.get("billed_lines")
        if bl is not None:
            for x in bl.to_dict("records"):
                inv.append(dict(customer=c.name, ns_id=c.ns_id, invoice=x["number"], issued=str(x["issue_date"]),
                                line=x["description"], qty=x["qty"], unit_cents=str(x["unit_amount_decimal"]),
                                amount=_f(x["amount_usd"]), service_from=str(x["ps"]), product=x["product_id"], price_id=x["price_id"]))
        lines = r.tables["invoice_lines"].set_index("id") if "invoice_lines" in r.tables else None
        for x in p.get("rows", []):
            if x["invoice"]:
                price = lines.loc[x["line_id"], "price_id"] if lines is not None and x["line_id"] in lines.index else ""
                inv.append(dict(customer=c.name, ns_id=c.ns_id, invoice=x["invoice"], issued=str(x["issue_date"]),
                                line=x["kind"], qty=1, unit_cents="", amount=_f(x["actual"]), service_from=str(x["period_start"]),
                                product="prod_platform", price_id=price))
    return dict(customers=pd.DataFrame(rows, columns=SNAPSHOT_COLUMNS), invoices=pd.DataFrame(inv))


def september_overrides(tables: Dict[str, pd.DataFrame]):
    """Booked September figures from the final September 30 balances and the revenue plan."""
    ob = tables["opening"]
    balances: Dict[str, Dict[str, Decimal]] = {}
    for x in ob.to_dict("records"):
        balances.setdefault(x["Customer Internal ID"], {})[x["Account Number"]] = D(x["Debit Balance"]) - D(x["Credit Balance"])
    accrued = {ns: b.get("1150", ZERO) for ns, b in balances.items()}   # Sept 30 unbilled usage = September accrual
    plan = tables["rev_plan"]
    revenue: Dict[str, Decimal] = {}
    for x in plan.to_dict("records"):
        if parse_period(x["Posting Period"]) == (2026, 9):
            revenue[x["Customer Internal ID"]] = revenue.get(x["Customer Internal ID"], ZERO) + D(x["Recognized Amount"])
    return accrued, revenue, balances
