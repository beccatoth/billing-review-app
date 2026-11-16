"""The review: expected vs billed vs booked, findings, proposed entries.

run_review() is the single entry point used by the app and the tests.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

import pandas as pd

from .terms import ContractTerms, build_contracts
from .util import (D, ZERO, add_months, cents_to_usd, days_between, fmt_int, fmt_usd, is_blank,
                   long_date, money, month_bounds, overlap_days, parse_date, parse_period, parse_ts,
                   period_label, to_denver, ts_to_service_date, whole)
from .validate import prepare_usage, run_checks

ACCOUNTS = {
    "1100": "Accounts receivable",
    "1150": "Unbilled usage receivable",
    "1160": "Platform contract asset",
    "2200": "Deferred platform revenue",
    "4000": "Platform revenue",
    "4010": "Usage revenue",
}

OWNER_BILLING = "Billing"
OWNER_REVACCT = "Revenue Accounting"
OWNER_PRODOPS = "Product Operations"
OWNER_SALESOPS = "Sales Ops / Deal Desk"

ERROR = "Error"
VALID = "Valid explanation"
NEEDS_INFO = "Needs information"
FOLLOW_UP = "Follow-up (later service)"


@dataclass
class Customer:
    sf_id: str
    name: str
    stripe_id: str
    ns_id: str


@dataclass
class Finding:
    id: str
    customer: str
    ns_id: str
    area: str                      # Usage / Platform / Data
    title: str
    classification: str            # Error / Valid explanation / Needs information / Follow-up
    period: str
    billing_effect: Decimal = ZERO   # + under-billed (bill more), - over-billed
    revenue_effect: Decimal = ZERO   # + October revenue understated, - overstated
    conditional_exposure: str = ""
    billing_action: str = ""
    accounting_action: str = ""
    why_billing_only: str = ""
    root_cause: str = ""
    calculation: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    next_step: str = ""
    owner: str = ""
    priority: str = "Low"
    controls: List[str] = field(default_factory=list)

    def as_row(self) -> dict:
        return dict(ID=self.id, Priority=self.priority, Customer=self.customer, Area=self.area,
                    Finding=self.title, Classification=self.classification, Period=self.period,
                    **{"Billing effect": float(self.billing_effect), "October revenue effect": float(self.revenue_effect),
                       "Conditional exposure (not in totals)": self.conditional_exposure,
                       "Billing action (Nov issue date)": self.billing_action,
                       "October accounting action": self.accounting_action,
                       "Billing vs revenue": self.why_billing_only,
                       "Root cause": self.root_cause, "Calculation": "\n".join(self.calculation),
                       "Evidence": "\n".join(self.evidence), "Recommended next step": self.next_step,
                       "Owner": self.owner, "Controls": ", ".join(self.controls)})


@dataclass
class ReviewResult:
    period: tuple
    cutoff: datetime
    customers: List[Customer]
    contracts: Dict[str, ContractTerms]
    checks: List[dict]
    blocking: List[str]
    usage: Dict[str, dict] = field(default_factory=dict)
    platform: Dict[str, dict] = field(default_factory=dict)
    findings: List[Finding] = field(default_factory=list)
    journals: List[dict] = field(default_factory=list)
    billing_docs: List[dict] = field(default_factory=list)
    plan_updates: List[dict] = field(default_factory=list)
    rollforward: List[dict] = field(default_factory=list)
    controls: List[dict] = field(default_factory=list)
    unverified: List[dict] = field(default_factory=list)
    amendment_status: Dict[str, tuple] = field(default_factory=dict)
    usage_prep: dict = field(default_factory=dict)
    tables: Dict[str, pd.DataFrame] = field(default_factory=dict)

    @property
    def period_label(self) -> str:
        return period_label(*self.period)


# ======================================================================= helpers
def _gl_prepared(gl: pd.DataFrame, cutoff: datetime) -> pd.DataFrame:
    g = gl.copy()
    g["dr"] = g["Debit"].map(D)
    g["cr"] = g["Credit"].map(D)
    g["net"] = g["dr"] - g["cr"]
    g["period"] = g["Posting Period"].map(parse_period)
    g["created"] = g["Created At"].map(parse_ts)
    return g[g["created"].map(lambda c: c is None or c <= cutoff)]


def _stripe_lines(t: Dict[str, pd.DataFrame], cutoff: datetime) -> pd.DataFrame:
    inv = t["invoices"].copy()
    inv["created_dt"] = inv["created"].map(parse_ts)
    inv = inv[inv["created_dt"] <= cutoff]
    inv = inv[~inv["status"].str.lower().isin(["void", "draft", "deleted"])]
    li = t["invoice_lines"].merge(inv, left_on="invoice_id", right_on="id", suffixes=("", "_inv"))
    li["ps"] = li["period_start"].map(ts_to_service_date)
    li["pe"] = li["period_end"].map(ts_to_service_date)
    li["amount_usd"] = li["amount"].map(cents_to_usd) - li["discount_amount"].map(cents_to_usd)
    li["unit_usd"] = li["unit_amount_decimal"].map(lambda x: D(x) / 100)
    li["qty"] = li["quantity"].map(lambda q: int(D(q)))
    li["issue_date"] = li["created_dt"].map(lambda d: to_denver(d).date())
    return li


def _issued_credits(t: Dict[str, pd.DataFrame], cutoff: datetime) -> pd.DataFrame:
    cn = t["credit_notes"].copy()
    cn["created_dt"] = cn["created"].map(parse_ts)
    cn = cn[(cn["created_dt"] <= cutoff)]
    cnl = t["credit_note_lines"].merge(cn, left_on="credit_note_id", right_on="id", suffixes=("", "_cn"))
    cnl["amount_usd"] = cnl["amount"].map(cents_to_usd)
    return cnl


def _match_approval(approvals: pd.DataFrame, name: str, kind: str, period_key: str) -> List[dict]:
    if approvals is None or approvals.empty:
        return []
    out = []
    for r in approvals.fillna("").to_dict("records"):
        cn = str(r.get("customer_name", "")).strip().lower()
        if r.get("type") == kind and str(r.get("service_month", ""))[:7] == period_key and cn and \
                (cn == name.lower() or cn.split(",")[0] == name.lower().split(",")[0]):
            out.append(r)
    return out


def _je(customer: Customer, finding_id: str, title: str, lines: List[tuple], calc: List[str], period_end: date):
    """lines = [(account, debit, credit, memo)]."""
    rows = [dict(account=a, account_name=ACCOUNTS[a], debit=money(dr), credit=money(cr), memo=m) for a, dr, cr, m in lines]
    return dict(je_id=f"PJE-{finding_id}", finding=finding_id, customer=customer.name, ns_id=customer.ns_id,
                date=period_end - timedelta(days=1), posting_period=None, title=title, lines=rows, calculation=calc,
                balanced=sum(r["debit"] for r in rows) == sum(r["credit"] for r in rows))


def _priority(f: Finding) -> str:
    if f.classification == ERROR and f.revenue_effect != 0:
        return "High"      # changes October's books
    if f.classification == ERROR:
        return "Medium"    # billing-only
    if f.classification in (NEEDS_INFO, FOLLOW_UP):
        return "Medium"
    return "Low"


def recognition_rows(c: ContractTerms, m0: date, m1: date) -> List[dict]:
    """Platform revenue for a month: days/365 per layer (base + upgrades), each rounded once;
    the final period absorbs rounding."""
    rec_rows = []
    for L in c.platform_layers():
        days = overlap_days(L["start"], L["end_excl"], m0, m1)
        if days == 0:
            continue
        amt = money(L["annual"] * days / 365)
        note = f"{fmt_usd(L['annual'])} × {days}/365 = {fmt_usd(amt)}"
        if L["end_excl"] <= m1:
            total = L["total"] if L["total"] is not None else money(L["annual"] * days_between(L["start"], L["end_excl"]) / 365)
            prior, d = ZERO, L["start"]
            while d < m0:
                nm = month_bounds(d.year, d.month)[1]
                prior += money(L["annual"] * overlap_days(L["start"], L["end_excl"], d, nm) / 365)
                d = nm
            amt = total - prior
            note += f"; final period absorbs rounding → {fmt_usd(amt)}"
        rec_rows.append(dict(layer=L["label"], doc=L["doc"].document_id, annual=L["annual"], days=days, amount=amt, calc=note))
    return rec_rows


# ======================================================================= main
def run_review(t: Dict[str, pd.DataFrame], terms_df: pd.DataFrame, approvals: pd.DataFrame,
               year: int, month: int, cutoff: datetime, expected_counts: Dict[str, int]) -> ReviewResult:
    m0, m1 = month_bounds(year, month)
    pkey = f"{year:04d}-{month:02d}"
    plabel = period_label(year, month)
    next_label = period_label(m1.year, m1.month)
    prev0 = add_months(m0, -1)

    customers = [Customer(r["AccountId"], r["AccountName"], r["StripeCustomerId__c"], r["NetSuiteCustomerInternalId__c"])
                 for r in t["accounts"].to_dict("records")]
    by_sf = {c.sf_id: c for c in customers}
    by_stripe = {c.stripe_id: c for c in customers}
    contracts, cproblems = build_contracts(terms_df, t["accounts"])
    up = prepare_usage(t["usage"])
    checks = run_checks(t, up, expected_counts, cutoff, cproblems, contracts)
    blocking = [c["detail"] for c in checks if c["blocking"]]
    res = ReviewResult((year, month), cutoff, customers, contracts, checks, blocking, usage_prep=up, tables=t)

    usage = up["clean"]
    usage = usage[usage["ingested_dt"] <= cutoff]
    mp = t["mapping"].copy()
    gl = _gl_prepared(t["gl"], cutoff)
    lines = _stripe_lines(t, cutoff)
    credits = _issued_credits(t, cutoff)
    plan = t["rev_plan"].copy()
    plan["period"] = plan["Posting Period"].map(parse_period)
    seq = [0]

    def fid():
        seq[0] += 1
        return f"F{seq[0]:02d}"

    # ------------------------------------------------------------- usage per customer
    def usage_for(sf_id: str, extra_units: int = 0, extra_label: str = "") -> dict:
        c = contracts.get(sf_id)
        pas = mp[(mp["salesforce_account_id"] == sf_id)]
        recs = []
        for r in pas.to_dict("records"):
            ef, et = parse_date(r["effective_from"]), parse_date(r["effective_to"]) or date(9999, 1, 1)
            sub = usage[(usage["product_account_id"] == r["product_account_id"]) &
                        (usage["service_date_d"] >= max(m0, ef)) & (usage["service_date_d"] < min(m1, et))]
            recs.append(sub)
        recs = pd.concat(recs) if recs else usage.iloc[0:0]
        units = int(recs["units_i"].sum()) + extra_units
        out = dict(records=recs, units=units, product_accounts=list(pas["product_account_id"]),
                   by_account={pa: int(recs.loc[recs["product_account_id"] == pa, "units_i"].sum()) for pa in pas["product_account_id"]})
        if c is None:
            out.update(allowance=0, excess=0, rate=ZERO, gross=ZERO, net=ZERO, concession=ZERO, calc=["No approved contract terms."])
            return out
        act0, act1 = max(m0, c.service_start), min(m1, c.service_end_excl)
        dim = days_between(m0, m1)
        segs, weighted = [], Decimal(0)
        d = act0
        while d < act1:
            a, src = c.allowance_on(d)
            e = d
            while e < act1 and c.allowance_on(e)[0] == a:
                e += timedelta(days=1)
            n = days_between(d, e)
            segs.append((a, n, src.document_id, d, e))
            weighted += Decimal(a) * n
            d = e
        allowance = whole(weighted / dim) if dim else 0
        rates = {c.rate_on(dd)[0] for dd in [act0, act1 - timedelta(days=1)]} if act1 > act0 else {c.rate_on(m0)[0]}
        rate, rsrc = c.rate_on(max(act0, min(act1 - timedelta(days=1), m1 - timedelta(days=1))) if act1 > act0 else m0)
        excess = max(0, units - allowance)
        gross = money(Decimal(excess) * rate)
        conc = _match_approval(approvals, by_sf[sf_id].name, "usage_concession", pkey)
        approved = [r for r in conc if str(r.get("status", "")).lower() == "approved"]
        concession = sum((D(r["amount"]) for r in approved), ZERO)
        net = max(ZERO, gross - concession)
        calc = []
        accts = ", ".join(f"{pa} {fmt_int(u)}" for pa, u in out["by_account"].items()) or "none"
        calc.append(f"Usage: {fmt_int(units - extra_units)} runs from {len(recs)} unique daily records ({accts})"
                    + (f" + {fmt_int(extra_units)} {extra_label}" if extra_units else ""))
        if len(segs) == 1 and segs[0][1] == dim:
            calc.append(f"Allowance: {fmt_int(segs[0][0])} runs (full month, {segs[0][2]})")
        else:
            parts = " + ".join(f"{fmt_int(a)} × {n}" for a, n, *_ in segs)
            calc.append(f"Allowance: ({parts}) / {dim} days = {fmt_int(allowance)} runs (day-weighted, rounded once)")
        calc.append(f"Excess: max(0, {fmt_int(units)} − {fmt_int(allowance)}) = {fmt_int(excess)} runs")
        calc.append(f"Charge: {fmt_int(excess)} × ${rate} ({rsrc.document_id}) = {fmt_usd(gross)}")
        if concession:
            calc.append(f"Less approved concession {', '.join(r['reference'] for r in approved)}: {fmt_usd(concession)} → net {fmt_usd(net)}")
        if len(rates) > 1:
            calc.append("Note: overage rate changes within the month – rate at month end applied; confirm with contract owner.")
        out.update(allowance=allowance, excess=excess, rate=rate, rate_doc=rsrc.document_id, gross=gross,
                   concession=concession, concession_refs=[r["reference"] for r in approved],
                   requests=[r for r in conc if str(r.get("status", "")).lower() == "requested"],
                   net=net, calc=calc, segments=segs)
        return out

    def usage_baseline(sf_id: str, units: int) -> tuple:
        """Charge under the order form alone (ignoring amendments) – used to confirm root cause."""
        c = contracts[sf_id]
        a = int(D(c.order.get("included_runs_per_month")))
        act0, act1 = max(m0, c.service_start), min(m1, c.service_end_excl)
        alw = whole(Decimal(a) * days_between(act0, act1) / days_between(m0, m1))
        return alw, money(Decimal(max(0, units - alw)) * D(c.order.get("overage_rate_per_run")))

    usage_lines = lines[(lines["product_id"] == "prod_workflow_run")]
    plat_lines = lines[(lines["product_id"] == "prod_platform")]

    for cust in customers:
        c = contracts.get(cust.sf_id)
        u = usage_for(cust.sf_id)
        ul = usage_lines[(usage_lines["customer_id"] == cust.stripe_id) & (usage_lines["ps"] >= m0) & (usage_lines["ps"] < m1)]
        ul_ids = set(ul["id"])
        cr = credits[credits["invoice_line_item_id"].isin(ul_ids)]
        cr_issued = cr[cr["status"].str.lower() == "issued"]
        cr_void = cr[cr["status"].str.lower() != "issued"]
        billed_gross = sum(ul["amount_usd"], ZERO)
        billed_credit = sum(cr_issued["amount_usd"], ZERO)
        billed_net = billed_gross - billed_credit
        # supplemental/adjustment lines (proration=true) correct a prior invoice; they are excluded from the
        # unit-rate and quantity checks but included in the billed amount
        ul_main = ul[ul["proration"].str.lower() != "true"]
        billed_qty = int(ul_main["qty"].sum()) if len(ul_main) else 0
        g_c = gl[gl["Customer Internal ID"] == cust.ns_id]
        acc_lines = g_c[(g_c["Account Number"] == "4010") & (g_c["period"] == (year, month))]
        accrued = -sum(acc_lines["net"], ZERO)
        # release of this month's accrual by next-month invoices / credits (GL, through cutoff)
        nxt = g_c[(g_c["Account Number"] == "1150") & (g_c["period"] == (m1.year, m1.month))]
        released = -sum(nxt["net"], ZERO)
        inv_created = ul_main["created_dt"].min() if len(ul_main) else None
        late = u["records"][u["records"]["ingested_dt"] > inv_created] if inv_created is not None else u["records"].iloc[0:0]
        res.usage[cust.ns_id] = dict(u, billed_lines=ul, billed_gross=billed_gross, billed_credit=billed_credit,
                                     billed_net=billed_net, billed_qty=billed_qty, credits=cr_issued, void_credits=cr_void,
                                     accrued=accrued, accrual_lines=acc_lines, released=released,
                                     residual_1150=accrued - released, late=late, invoice_created=inv_created)

    # ------------------------------------------------------------- platform per customer
    for cust in customers:
        c = contracts.get(cust.sf_id)
        if c is None:
            continue
        sched = [s for s in c.platform_invoice_schedule() if s["period_start"] < m1 and s["period_end"] > m0]
        later = [s for s in c.platform_invoice_schedule() if s["period_start"] >= m1 and s["invoice_date"] and s["invoice_date"] <= to_denver(cutoff).date()]
        pl = plat_lines[plat_lines["customer_id"] == cust.stripe_id]
        cover = pl[(pl["ps"] < m1) & (pl["pe"] > m0)]
        rows, used = [], set()
        for s in sched:
            cand = cover[(cover["ps"] == s["period_start"]) & (~cover["id"].isin(used))]
            if s["kind"].startswith("Upgrade"):
                cand = cand[(cand["proration"].str.lower() == "true") | (cand["description"].str.contains("catch|upgrade|prorat", case=False))]
            else:
                cand = cand[cand["proration"].str.lower() != "true"]
            act = cand.iloc[0] if len(cand) else None
            if act is not None:
                used.add(act["id"])
            rows.append(dict(kind=s["kind"], source=s["source"], period_start=s["period_start"], period_end=s["period_end"],
                             expected=s["amount"], actual=(act["amount_usd"] if act is not None else ZERO),
                             invoice=(act["number"] if act is not None else ""), line_id=(act["id"] if act is not None else ""),
                             issue_date=(act["issue_date"] if act is not None else None),
                             oct_days=overlap_days(s["period_start"], s["period_end"], m0, m1),
                             period_days=days_between(s["period_start"], s["period_end"])))
        for a in cover[~cover["id"].isin(used)].to_dict("records"):
            rows.append(dict(kind="Unexpected platform charge", source="", period_start=a["ps"], period_end=a["pe"],
                             expected=ZERO, actual=a["amount_usd"], invoice=a["number"], line_id=a["id"], issue_date=a["issue_date"],
                             oct_days=overlap_days(a["ps"], a["pe"], m0, m1), period_days=days_between(a["ps"], a["pe"])))
        for r in rows:
            r["alloc_expected"] = money(r["expected"] * r["oct_days"] / r["period_days"]) if r["period_days"] else ZERO
            r["alloc_actual"] = money(r["actual"] * r["oct_days"] / r["period_days"]) if r["period_days"] else ZERO
        rec_rows = recognition_rows(c, m0, m1)
        expected_rev = sum((r["amount"] for r in rec_rows), ZERO)
        p_c = plan[(plan["Customer Internal ID"] == cust.ns_id) & (plan["period"] == (year, month))]
        planned = sum(p_c["Planned Amount"].map(D), ZERO)
        plan_docs = sorted(set(plan[plan["Customer Internal ID"] == cust.ns_id]["Source Document ID"]))
        g_c = gl[gl["Customer Internal ID"] == cust.ns_id]
        gl_rev = -sum(g_c[(g_c["Account Number"] == "4000") & (g_c["period"] == (year, month))]["net"], ZERO)
        later_rows = []
        for s in later:
            act = pl[(pl["ps"] == s["period_start"]) & (pl["proration"].str.lower() != "true")]
            later_rows.append(dict(s, actual=(act["amount_usd"].iloc[0] if len(act) else None),
                                   invoice=(act["number"].iloc[0] if len(act) else "")))
        res.platform[cust.ns_id] = dict(rows=rows, recognition=rec_rows, expected_rev=expected_rev, planned=planned,
                                        gl_rev=gl_rev, plan_docs=plan_docs, plan_rows=p_c, later=later_rows,
                                        expected_cover=sum((r["expected"] for r in rows), ZERO),
                                        actual_cover=sum((r["actual"] for r in rows), ZERO),
                                        billed_in_month=sum((r["actual"] for r in rows if r["issue_date"] and m0 <= r["issue_date"] < m1), ZERO),
                                        expected_billed_in_month=sum((r["expected"] for r in rows if (r["period_start"] >= m0 and r["period_start"] < m1)), ZERO))

    # ------------------------------------------------------------- amendments: reflected in billing? (Stripe evidence)
    usage_fields = ("included_runs_per_month", "overage_rate_per_run")
    platform_fields = ("platform_annual_fee", "platform_invoice_amount", "catch_up_charge")
    for cust in customers:
        c = contracts.get(cust.sf_id)
        if not c:
            continue
        u, p = res.usage[cust.ns_id], res.platform.get(cust.ns_id, {})
        for a in c.amendments:
            if a.effective_from >= m1:
                a.billing_status, a.billing_evidence = "Not yet effective", ""
                continue
            sts, ev = [], []
            if any(a.has(f_) for f_ in usage_fields):
                bl = u["billed_lines"]
                main = bl[bl["proration"].str.lower() != "true"] if len(bl) else bl
                if not len(main):
                    sts.append("Not yet billed")
                else:
                    billed_main = sum(main["amount_usd"], ZERO)
                    alw0, ch0 = usage_baseline(cust.sf_id, u["units"])
                    inv = ", ".join(main["number"])
                    if billed_main == u["gross"]:
                        sts.append("Reflected")
                        ev.append(f"{inv} billed on amended terms ({fmt_int(u['allowance'])} runs allowance)")
                    elif billed_main == ch0:
                        sts.append("Corrected" if u["billed_net"] == u["net"] else "Not reflected")
                        ev.append(f"{inv} billed on the original allowance of {fmt_int(alw0)} runs ({fmt_usd(ch0)})")
                    else:
                        sts.append("Not reflected")
                        ev.append(f"{inv} billed {fmt_usd(billed_main)} vs {fmt_usd(u['gross'])} on amended terms")
            if any(a.has(f_) for f_ in platform_fields):
                cu = [r_ for r_ in p.get("rows", []) if r_["kind"].startswith("Upgrade") and r_["source"] == a.document_id]
                later = [s_ for s_ in p.get("later", []) if s_["source"] == a.document_id and s_["actual"] is not None]
                if not cu and not later:
                    sts.append("Not yet billed")
                for r_ in cu:
                    if r_["actual"] == r_["expected"]:
                        sts.append("Reflected"); ev.append(f"Catch-up {fmt_usd(r_['expected'])} billed ({r_['invoice']})")
                    else:
                        sts.append("Not reflected"); ev.append(f"Catch-up {fmt_usd(r_['expected'])} not billed")
                for s_ in later:
                    if s_["actual"] == s_["amount"]:
                        sts.append("Reflected"); ev.append(f"{s_['invoice']} at {fmt_usd(s_['amount'])}")
                    else:
                        sts.append("Not reflected"); ev.append(f"{s_['invoice']} billed {fmt_usd(s_['actual'])} vs {fmt_usd(s_['amount'])}")
            a.billing_status = next((x for x in ("Not reflected", "Corrected", "Reflected") if x in sts), "Not yet billed")
            a.billing_evidence = "; ".join(ev)
    res.amendment_status = {a.document_id: (a.billing_status, a.billing_evidence)
                            for c in contracts.values() for a in c.amendments}

    # ------------------------------------------------------------- findings: usage
    nov_issue = "November issue date, on approval"
    for cust in customers:
        u = res.usage[cust.ns_id]
        c = contracts.get(cust.sf_id)
        if c is None:
            continue
        exp = u["net"]
        bill_diff = exp - u["billed_net"]
        acc_diff = exp - u["accrued"]
        ev = [f"Contract: {c.order.document_id} – {fmt_int(int(D(c.order.get('included_runs_per_month'))))} runs/month, "
              f"${c.order.get('overage_rate_per_run')}/excess run"]
        for a in c.amendments:
            ev.append(f"Amendment: {a.document_id} effective {a.effective_from} – {a.billing_status.lower()} in billing"
                      + (f" ({a.billing_evidence})" if a.billing_evidence else ""))
        for r in u["billed_lines"].to_dict("records"):
            ev.append(f"Stripe {r['number']} ({r['issue_date']}): {fmt_int(r['qty'])} runs × {r['unit_amount_decimal']}¢ "
                      f"(price {r['price_id']}) = {fmt_usd(r['amount_usd'])}")
        for r in u["credits"].to_dict("records"):
            ev.append(f"Credit note {r['number']} (issued): {fmt_usd(r['amount_usd'])} – {r['description']}")
        for r in u["accrual_lines"].to_dict("records"):
            ev.append(f"GL {r['Document Number']} ({r['Posting Period']}, created {str(r['Created At'])[:10]}): Cr 4010 {fmt_usd(r['cr'] - r['dr'])}")
        if bill_diff == 0 and acc_diff == 0:
            # valid explanations worth surfacing
            if u["concession"]:
                f = Finding(fid(), cust.name, cust.ns_id, "Usage", "Approved usage concession correctly reflected", VALID, plabel,
                            calculation=u["calc"], evidence=ev + [f"Approval: {', '.join(u['concession_refs'])}"],
                            root_cause="Controller-approved reduction of October usage consideration.",
                            why_billing_only="Accrual already net of the concession; the November credit note reduces AR and reverses the 1150 release (Dr 1150 / Cr 1100) – no second revenue reduction.",
                            next_step="None. Confirm no second concession is applied.", owner=OWNER_REVACCT,
                            controls=["Credit notes: issued only, not double-counted"])
                res.findings.append(f)
            if exp == 0 and u["billed_net"] == 0 and len(u["product_accounts"]):
                res.findings.append(Finding(fid(), cust.name, cust.ns_id, "Usage", "No usage invoice – usage within allowance", VALID, plabel,
                                            calculation=u["calc"], evidence=ev, root_cause="Calculated overage is zero, so no usage invoice or accrual is required.",
                                            next_step="None.", owner=OWNER_BILLING, controls=["Usage invoice completeness"]))
            continue
        f = Finding(fid(), cust.name, cust.ns_id, "Usage", "", ERROR, plabel, billing_effect=bill_diff, revenue_effect=acc_diff,
                    calculation=list(u["calc"]), evidence=ev)
        causes, controls = [], []
        unloaded = [a for a in c.amendments if a.billing_status in ("Not reflected", "Corrected")
                    and any(a.has(f_) for f_ in usage_fields)]
        if unloaded:
            alw0, ch0 = usage_baseline(cust.sf_id, u["units"])
            confirmed = ch0 == u["billed_gross"]
            causes.append(f"Executed amendment {', '.join(a.document_id for a in unloaded)} is not reflected in billing: "
                          f"the invoice used the original allowance of {fmt_int(alw0)} runs → {fmt_usd(ch0)}"
                          + (" – exactly what was invoiced." if confirmed else "."))
            controls.append("Amendments reflected in billing")
            f.title = "Usage billed on pre-amendment terms"
        main_lines = u["billed_lines"][u["billed_lines"]["proration"].str.lower() != "true"]
        rate_bad = main_lines[main_lines["unit_usd"] != u.get("rate", ZERO)]
        if len(rate_bad):
            r = rate_bad.iloc[0]
            prior = usage_lines[(usage_lines["price_id"] == r["price_id"]) & (usage_lines["id"] != r["id"])]
            hist = "; ".join(f"{p['number']} {p['unit_amount_decimal']}¢" for p in prior.to_dict("records"))
            causes.append(f"Invoice {r['number']} billed {r['unit_amount_decimal']}¢ per run vs contract ${u['rate']} "
                          f"({u['rate'] * 100:.2f}¢). Same price ID {r['price_id']} previously billed: {hist or 'n/a'} – the price was changed in Stripe.")
            controls.append("Billed rate matches contract rate")
            f.title = f.title or "Usage billed at the wrong unit price"
        if not len(u["billed_lines"]) and exp > 0:
            causes.append(f"No usage invoice for {plabel} service was issued through the cutoff, although calculated overage is {fmt_int(u['excess'])} runs.")
            controls.append("Usage invoice completeness")
            f.title = f.title or "Usage overage never invoiced"
        if len(u["late"]) and len(u["billed_lines"]):
            lu = int(u["late"]["units_i"].sum())
            if u["billed_qty"] + lu == u["excess"] or not causes:
                causes.append(f"{len(u['late'])} usage record(s) ({fmt_int(lu)} runs; service {', '.join(str(d) for d in u['late']['service_date_d'])}) "
                              f"were ingested {', '.join(str(to_denver(x))[:16] for x in u['late']['ingested_dt'])} – after the usage invoice was created "
                              f"({str(to_denver(u['invoice_created']))[:16]}). The invoice missed them; the accrual included them.")
                controls.append("Late-ingested usage")
                f.title = f.title or "Usage invoice missed late-arriving usage"
        if len(u["billed_lines"]) and u["billed_qty"] != u["excess"] and not unloaded and not len(u["late"]):
            causes.append(f"Billed quantity {fmt_int(u['billed_qty'])} runs vs calculated excess {fmt_int(u['excess'])} runs.")
            controls.append("Billed quantity matches calculated excess")
            f.title = f.title or "Usage quantity billed differs from metered usage"
        if acc_diff != 0:
            if u["accrued"] == 0:
                causes.append(f"No {plabel} usage accrual was posted.")
                controls.append("Usage accrual completeness")
            elif u["accrued"] in (u["billed_gross"], u["billed_net"]):
                causes.append(f"The {plabel} accrual ({fmt_usd(u['accrued'])}) was built from the billed amount, so it carries the same error.")
                controls.append("Accrual recalculated from contract, not copied from invoice")
            else:
                causes.append(f"Accrual {fmt_usd(u['accrued'])} differs from recalculated {fmt_usd(exp)}.")
                controls.append("Accrual equals recalculated usage")
        f.title = f.title or "Usage difference"
        f.root_cause = " ".join(causes)
        f.controls = controls
        f.calculation.append(f"Billed for {plabel} usage (net of issued credits): {fmt_usd(u['billed_net'])} → billing difference {fmt_usd(bill_diff, True)}")
        f.calculation.append(f"Accrued in GL 4010 for {plabel}: {fmt_usd(u['accrued'])} → revenue difference {fmt_usd(acc_diff, True)}")
        # actions
        existing_fix = u["credits"] if bill_diff < 0 else None
        if bill_diff > 0:
            f.billing_action = (f"Issue supplemental usage invoice for {plabel} service: {fmt_usd(bill_diff)} ({nov_issue})."
                                if len(u["billed_lines"]) else f"Issue {plabel} usage invoice: {fmt_int(u['excess'])} runs × ${u['rate']} = {fmt_usd(bill_diff)} ({nov_issue}).")
        elif bill_diff < 0:
            f.billing_action = f"Issue credit note linked to {u['billed_lines']['number'].iloc[0]} for {fmt_usd(-bill_diff)} ({nov_issue})."
        if acc_diff > 0:
            f.accounting_action = f"October JE: Dr 1150 / Cr 4010 {fmt_usd(acc_diff)}."
        elif acc_diff < 0:
            f.accounting_action = f"October JE: Dr 4010 / Cr 1150 {fmt_usd(-acc_diff)}."
        else:
            f.accounting_action = "None – October usage revenue is already correct."
        if bill_diff != 0 and acc_diff == 0:
            f.why_billing_only = (f"Billing correction only. The {plabel} accrual already equals the contract amount, so October revenue is right; "
                                  f"the {fmt_usd(abs(bill_diff))} sits in 1150 until the {'supplemental invoice' if bill_diff > 0 else 'credit note'} is issued in {next_label} (Dr 1100 / Cr 1150).")
        elif bill_diff != 0:
            f.why_billing_only = (f"October revenue is corrected by the October JE. The {next_label} billing document then only moves the balance between 1150 and 1100 "
                                  f"({'Dr 1100 / Cr 1150' if bill_diff > 0 else 'Dr 1150 / Cr 1100'}) – it must not be recorded as revenue again.")
        else:
            f.why_billing_only = "Accounting-only: customer billing is already correct."
        f.next_step = " ".join(x for x in [
            "Billing: " + f.billing_action if f.billing_action else "",
            "Revenue Accounting: post the October JE after Controller approval." if acc_diff else "",
            "Billing Ops: load the amendment terms into Stripe before the next usage run." if unloaded else "",
            "Billing Ops: restore the contract price on the Stripe price ID and audit other customers using it." if len(rate_bad) else ""] if x)
        f.owner = " + ".join(sorted({OWNER_BILLING if bill_diff else "", OWNER_REVACCT if acc_diff else ""} - {""}))
        dup_note = ("Duplicate-correction check: no issued credit note or supplemental invoice for this service period exists through the cutoff."
                    if not len(u["credits"]) else f"Duplicate-correction check: issued credits of {fmt_usd(u['billed_credit'])} already netted above.")
        f.evidence.append(dup_note)
        res.findings.append(f)
        # proposals
        if acc_diff:
            dr, cr = ("1150", "4010") if acc_diff > 0 else ("4010", "1150")
            res.journals.append(_je(cust, f.id, f"{plabel} usage accrual correction – {cust.name}",
                                    [(dr, abs(acc_diff), ZERO, f"{plabel} usage: recalculated {fmt_usd(exp)} vs accrued {fmt_usd(u['accrued'])}"),
                                     (cr, ZERO, abs(acc_diff), f"{plabel} usage accrual correction")], f.calculation, m1))
        if bill_diff:
            res.billing_docs.append(dict(finding=f.id, customer=cust.name,
                                         type="Supplemental invoice" if bill_diff > 0 else "Credit note (linked)",
                                         linked_to=(u["billed_lines"]["number"].iloc[0] if len(u["billed_lines"]) else "—"),
                                         service_period=f"{m0} to {m1 - timedelta(days=1)}", amount=abs(bill_diff),
                                         issue_date=f"{next_label} (on approval)",
                                         gl_when_issued=("Dr 1100 / Cr 1150" if bill_diff > 0 else "Dr 1150 / Cr 1100") + " – not revenue",
                                         description=f.billing_action))

    # duplicate rows explained (valid) – show the effect they would have had
    dups = up["exact_dupes"]
    if len(dups):
        for pa, grp in dups.groupby("product_account_id"):
            row = mp[mp["product_account_id"] == pa]
            sf = row["salesforce_account_id"].iloc[0] if len(row) else ""
            if sf not in by_sf:
                continue
            cust = by_sf[sf]
            extra = sum(int(D(x)) for x in grp["units"])
            in_month = grp[grp["service_date"].map(parse_date).map(lambda d: d is not None and m0 <= d < m1)]
            if not len(in_month):
                continue
            w = usage_for(sf, extra_units=extra, extra_label="duplicate runs")
            u = res.usage[cust.ns_id]
            res.findings.append(Finding(fid(), cust.name, cust.ns_id, "Data", "Duplicate usage record excluded – billing correct", VALID, plabel,
                                        calculation=u["calc"] + [f"If the duplicate were counted: {fmt_usd(w['gross'])} (overstated by {fmt_usd(w['gross'] - u['gross'])})"],
                                        evidence=[f"record_id {r['record_id']} appears twice, identical in every field ({r['service_date']}, {fmt_int(int(D(r['units'])))} runs, {r['batch_id']})" for r in grp.to_dict("records")]
                                        + [f"Billed {fmt_usd(u['billed_net'])}; accrued {fmt_usd(u['accrued'])} – both on de-duplicated usage."],
                                        root_cause="Exact repeated source record in the metering export. Deduplicated on record_id; batch controls tie only after removal.",
                                        next_step="None for billing. Ask the metering team to prevent duplicate exports.", owner=OWNER_PRODOPS,
                                        controls=["Usage de-duplication", "Batch control tie-out"]))

    # void credit notes (valid)
    void_all = credits[credits["status"].str.lower() != "issued"]
    for r in void_all.to_dict("records"):
        cust = by_stripe.get(r["customer_id"])
        if not cust:
            continue
        posted = gl[gl["External Source ID"] == r["credit_note_id"]]
        res.findings.append(Finding(fid(), cust.name, cust.ns_id, "Usage", "Void credit note has no effect", VALID, plabel,
                                    evidence=[f"Credit note {r['number']} {fmt_usd(r['amount_usd'])} status '{r['status']}', voided {r['voided_at']}",
                                              "Not posted to NetSuite." if posted.empty else "WARNING: posted to NetSuite – reverse."],
                                    root_cause="Created in error and voided; only issued credit notes affect billing or accounting.",
                                    next_step="None." if posted.empty else "Reverse the GL posting.", owner=OWNER_BILLING,
                                    controls=["Credit notes: issued only"]))

    # unapproved concession requests (needs information – no effect)
    for cust in customers:
        u = res.usage[cust.ns_id]
        for r in u.get("requests", []):
            res.findings.append(Finding(fid(), cust.name, cust.ns_id, "Usage", "Concession requested but not approved – no effect", NEEDS_INFO, plabel,
                                        evidence=[f"{r['reference']}: {r['summary']}"],
                                        root_cause="A request alone does not authorise a credit or revenue reduction.",
                                        conditional_exposure=f"Up to {fmt_usd(D(r['amount']))} if approved",
                                        next_step="Controller decision required before any credit is issued.", owner="Controller / Sales",
                                        controls=["Unsupported corrections blocked"]))

    # ------------------------------------------------------------- unverified usage
    unv = mp[mp["salesforce_account_id"].str.strip() == ""]
    for r in unv.to_dict("records"):
        recs = usage[(usage["product_account_id"] == r["product_account_id"]) & (usage["service_date_d"] >= m0) & (usage["service_date_d"] < m1)]
        units = int(recs["units_i"].sum())
        prev_units = int(usage[(usage["product_account_id"] == r["product_account_id"]) & (usage["service_date_d"] >= prev0) & (usage["service_date_d"] < m0)]["units_i"].sum())
        scen = []
        for cust in customers:
            if cust.sf_id not in contracts:
                continue
            w = usage_for(cust.sf_id, extra_units=units, extra_label="unverified runs")
            scen.append(w["gross"] - res.usage[cust.ns_id]["gross"])
        lo, hi = (min(scen), max(scen)) if scen else (ZERO, ZERO)
        res.unverified.append(dict(product_account_id=r["product_account_id"], label=r["account_label"], units=units,
                                   prev_units=prev_units, low=lo, high=hi, records=len(recs)))
        res.findings.append(Finding(fid(), "Unassigned workspace", "", "Data", f"Usage on unverified workspace {r['product_account_id']}", NEEDS_INFO, plabel,
                                    conditional_exposure=f"{fmt_usd(lo)}–{fmt_usd(hi)} depending on owner; $0 if internal",
                                    calculation=[f"{fmt_int(units)} runs across {len(recs)} daily records in {plabel} ({fmt_int(prev_units)} in the prior month)",
                                                 "Range = extra overage if these runs were added to each customer's own usage under that customer's contract (min–max across all customers)."],
                                    evidence=[f"Registry: {r['product_account_id']} '{r['account_label']}', no Salesforce account, effective {r['effective_from']}",
                                              "MSG-2026-131: do not identify by elimination; Product Operations must verify ownership.",
                                              "MSG-2026-132: a concession request does not establish ownership."],
                                    root_cause="Workspace has metered usage but no verified billing owner.",
                                    next_step="Product Operations to provide ownership evidence (signup record, admin user domain, contract reference). Billing assigns and invoices only after verification; Revenue Accounting then assesses whether an accrual is required.",
                                    owner=OWNER_PRODOPS, controls=["Unverified usage isolated"]))
    for cust in customers:
        if cust.sf_id in contracts and not len(mp[mp["salesforce_account_id"] == cust.sf_id]):
            res.findings.append(Finding(fid(), cust.name, cust.ns_id, "Data", "Active contract but no verified product account", NEEDS_INFO, plabel,
                                        evidence=[f"Contract {contracts[cust.sf_id].order.document_id} in force; no row in product_account_mapping for {cust.sf_id}.",
                                                  "Platform billing is unaffected."],
                                        root_cause="No usage can be attributed to this customer until a workspace is verified.",
                                        next_step="Product Operations to confirm the customer's workspace. Do not assume the unverified workspace is theirs.",
                                        owner=OWNER_PRODOPS, controls=["Customers without product accounts"]))

    # ------------------------------------------------------------- findings: platform
    for cust in customers:
        c = contracts.get(cust.sf_id)
        p = res.platform.get(cust.ns_id)
        if not c or not p:
            continue
        unloaded = [a for a in c.amendments if a.billing_status in ("Not reflected", "Corrected")
                    and any(a.has(f_) for f_ in platform_fields)]
        missing_docs = [a.document_id for a in c.amendments if a.has("platform_annual_fee") and a.effective_from < m1 and a.document_id not in p["plan_docs"]]
        bill_diff = p["expected_cover"] - p["actual_cover"]
        rev_diff = p["expected_rev"] - p["gl_rev"]
        if bill_diff == 0 and rev_diff == 0 and p["planned"] == p["gl_rev"]:
            continue
        f = Finding(fid(), cust.name, cust.ns_id, "Platform", "", ERROR, plabel, billing_effect=bill_diff, revenue_effect=rev_diff)
        f.calculation = [f"Recognition ({r['layer']}): {r['calc']}" for r in p["recognition"]]
        f.calculation.append(f"Expected {plabel} platform revenue {fmt_usd(p['expected_rev'])}; revenue plan {fmt_usd(p['planned'])}; GL 4000 {fmt_usd(p['gl_rev'])} → {fmt_usd(rev_diff, True)}")
        for r in p["rows"]:
            f.calculation.append(f"{r['kind']} {r['period_start']}–{r['period_end'] - timedelta(days=1)}: expected {fmt_usd(r['expected'])}, billed {fmt_usd(r['actual'])} {r['invoice']}")
        f.evidence = [f"{a.document_id} effective {a.effective_from}: {a.billing_status.lower()} in billing"
                      + (f" ({a.billing_evidence})" if a.billing_evidence else "") for a in c.amendments]
        f.evidence.append(f"Revenue plan source documents: {', '.join(p['plan_docs'])} (updated {p['plan_rows']['Plan Updated At'].iloc[0][:10] if len(p['plan_rows']) else 'n/a'})")
        causes = []
        if unloaded:
            causes.append(f"Amendment {', '.join(a.document_id for a in unloaded)} not reflected in billing: "
                          + "; ".join(a.billing_evidence for a in unloaded if a.billing_evidence) + ".")
        if missing_docs:
            causes.append(f"Revenue plan built only from {', '.join(p['plan_docs'])}; {', '.join(missing_docs)} (platform price change) not added.")
        f.root_cause = " ".join(causes) or "Platform billing or recognition differs from contract."
        f.title = "Platform upgrade not billed or recognised" if (unloaded or missing_docs) else "Platform billing / recognition difference"
        f.controls = (["Amendments reflected in billing"] if unloaded else []) + (["Revenue plan includes all amendments"] if missing_docs else []) + ["Platform recognition = contract days/365"]
        if bill_diff > 0:
            miss = [r for r in p["rows"] if r["expected"] > r["actual"]]
            f.billing_action = "; ".join(f"Issue supplemental invoice for {r['kind'].lower()} {fmt_usd(r['expected'] - r['actual'])} (service {r['period_start']}–{r['period_end'] - timedelta(days=1)}, {nov_issue})" for r in miss)
            for r in miss:
                res.billing_docs.append(dict(finding=f.id, customer=cust.name, type="Supplemental invoice", linked_to=r["source"],
                                             service_period=f"{r['period_start']} to {r['period_end'] - timedelta(days=1)}", amount=r["expected"] - r["actual"],
                                             issue_date=f"{next_label} (on approval)", gl_when_issued="Dr 1100 / Cr 2200 (releases the 1160 contract asset as billed)",
                                             description=r["kind"]))
        elif bill_diff < 0:
            f.billing_action = f"Issue linked credit note {fmt_usd(-bill_diff)} ({nov_issue})."
        # recognition JE and incremental reclass
        if rev_diff:
            dr, cr = ("2200", "4000") if rev_diff > 0 else ("4000", "2200")
            res.journals.append(_je(cust, f.id, f"{plabel} platform recognition – {cust.name}",
                                    [(dr, abs(rev_diff), ZERO, f"{plabel} platform recognition correction"),
                                     (cr, ZERO, abs(rev_diff), "; ".join(r["calc"] for r in p["recognition"] if r["layer"].startswith("Upgrade")) or "recognition correction")],
                                    f.calculation, m1))
        f.accounting_action = (f"October JE: {'Dr 2200 / Cr 4000' if rev_diff > 0 else 'Dr 4000 / Cr 2200'} {fmt_usd(abs(rev_diff))}; then recompute the contract-asset reclass (see roll-forward)."
                               if rev_diff else "None – October platform revenue already correct.")
        f.why_billing_only = ("Revenue follows service days (days/365), not invoices: October revenue is corrected in October regardless of when the catch-up is billed. "
                              f"The {next_label} supplemental invoice is Dr 1100 / Cr 2200 – not revenue again." if rev_diff and bill_diff else
                              "Billing-only: recognition already correct." if bill_diff else "")
        f.next_step = " ".join(x for x in [
            "Revenue Accounting: post recognition + incremental contract-asset JEs after approval." if rev_diff else "",
            f"Billing: {f.billing_action}" if f.billing_action else "",
            "Billing Ops: load the amendment to Stripe before the next platform invoice." if unloaded else "",
            "Revenue Accounting: add the amendment as an incremental revenue element (see Revenue plan updates)." if missing_docs else ""] if x)
        f.owner = " + ".join(sorted({OWNER_REVACCT if rev_diff or missing_docs else "", OWNER_BILLING if bill_diff else ""} - {""}))
        f.evidence.append("Duplicate-correction check: no catch-up or supplemental platform invoice found through the cutoff.")
        res.findings.append(f)
        for a in c.amendments:
            if a.document_id in missing_docs:
                inc = [L for L in c.platform_layers() if L["doc"] is a][0]
                total = inc["total"] if inc["total"] is not None else money(inc["annual"] * days_between(inc["start"], inc["end_excl"]) / 365)
                res.plan_updates.append(dict(customer=cust.name, finding=f.id, source_document=a.document_id,
                                             recommendation=f"Add incremental revenue element for {a.document_id}: {fmt_usd(total)} over "
                                                            f"{days_between(inc['start'], inc['end_excl'])} days ({inc['start']} to {inc['end_excl'] - timedelta(days=1)}), "
                                                            f"{fmt_usd(inc['annual'])}/yr × days/365 per month, final period absorbs rounding. "
                                                            f"{plabel}: {[r['amount'] for r in p['recognition'] if r['doc'] == a.document_id][0]:,.2f}. Keep the base element unchanged."))
        # later-service follow-ups
    for cust in customers:
        p = res.platform.get(cust.ns_id)
        if not p:
            continue
        for s in p["later"]:
            if s["actual"] is not None and s["actual"] != s["amount"]:
                res.findings.append(Finding(fid(), cust.name, cust.ns_id, "Platform", f"{period_label(s['period_start'].year, s['period_start'].month)} platform invoice at old price", FOLLOW_UP,
                                            period_label(s["period_start"].year, s["period_start"].month),
                                            evidence=[f"{s['invoice']}: billed {fmt_usd(s['actual'])}; contract ({s['source']}) {fmt_usd(s['amount'])} for service from {s['period_start']}"],
                                            root_cause="Same unloaded amendment – later-service billing also wrong.",
                                            conditional_exposure=f"{fmt_usd(s['amount'] - s['actual'])} {period_label(s['period_start'].year, s['period_start'].month)} billing gap",
                                            next_step=f"Billing: correct as part of the {next_label} billing review (supplemental invoice). No {plabel} accounting effect.",
                                            owner=OWNER_BILLING, controls=["Amendments reflected in billing"]))

    # ------------------------------------------------------------- prior-month accrual release check
    for cust in customers:
        g_c = gl[gl["Customer Internal ID"] == cust.ns_id]
        ob = t["opening"]
        o = ob[(ob["Customer Internal ID"] == cust.ns_id) & (ob["Account Number"] == "1150")]
        open1150 = (D(o["Debit Balance"].iloc[0]) - D(o["Credit Balance"].iloc[0])) if len(o) else ZERO
        rel = -sum(g_c[(g_c["Account Number"] == "1150") & (g_c["period"] == (year, month)) & (g_c["Transaction Type"] != "Journal")]["net"], ZERO)
        if open1150 != rel:
            res.findings.append(Finding(fid(), cust.name, cust.ns_id, "Usage", "Prior-month usage accrual not fully released", NEEDS_INFO,
                                        period_label(prev0.year, prev0.month), billing_effect=ZERO,
                                        evidence=[f"Opening 1150 {fmt_usd(open1150)}; released by {plabel} invoices {fmt_usd(rel)}"],
                                        conditional_exposure=f"{fmt_usd(open1150 - rel)} prior-period residual in 1150",
                                        next_step="Revenue Accounting to investigate the prior-month residual.", owner=OWNER_REVACCT,
                                        controls=["Prior accrual released"]))

    # ------------------------------------------------------------- roll-forward + contract asset
    adj_by = defaultdict(lambda: defaultdict(lambda: ZERO))
    for j in res.journals:
        for l in j["lines"]:
            adj_by[j["ns_id"]][l["account"]] += l["debit"] - l["credit"]
    ob = t["opening"]
    for cust in customers:
        g_c = gl[gl["Customer Internal ID"] == cust.ns_id]
        oct_ = g_c[g_c["period"] == (year, month)]
        nov_ = g_c[g_c["period"] == (m1.year, m1.month)]
        reclass_jes = set(oct_.loc[oct_["Account Number"] == "1160", "Internal ID"])
        def opening(a):
            o = ob[(ob["Customer Internal ID"] == cust.ns_id) & (ob["Account Number"] == a)]
            return (D(o["Debit Balance"].iloc[0]) - D(o["Credit Balance"].iloc[0])) if len(o) else ZERO
        pre = opening("2200") + opening("1160") + sum(oct_[(oct_["Account Number"] == "2200") & (~oct_["Internal ID"].isin(reclass_jes))]["net"], ZERO) + adj_by[cust.ns_id]["2200"]
        required = max(ZERO, pre)
        existing = opening("1160") + sum(oct_[oct_["Account Number"] == "1160"]["net"], ZERO)
        inc = required - existing
        if inc != 0:
            fids = [f.id for f in res.findings if f.ns_id == cust.ns_id and f.area == "Platform" and f.classification == ERROR]
            key = fids[0] if fids else fid()
            j = _je(cust, key, f"{plabel} contract asset reclass (incremental) – {cust.name}",
                    [("1160" if inc > 0 else "2200", abs(inc), ZERO, "Incremental platform contract asset after recognition correction"),
                     ("2200" if inc > 0 else "1160", ZERO, abs(inc), "Incremental platform contract asset after recognition correction")],
                    [f"2200 before reclass, after correction: {fmt_usd(pre)} {'debit' if pre > 0 else 'credit'} → required 1160 {fmt_usd(required)}",
                     f"1160 already booked: {fmt_usd(existing)} → incremental {fmt_usd(inc, True)}"], m1)
            j["je_id"] += "-CA"
            res.journals.append(j)
            adj_by[cust.ns_id]["1160"] += inc
            adj_by[cust.ns_id]["2200"] -= inc
        for a in ("1100", "1150", "1160", "2200", "4000", "4010"):
            op = opening(a) if a in ("1100", "1150", "1160", "2200") else ZERO
            act = sum(oct_[oct_["Account Number"] == a]["net"], ZERO)
            adj = adj_by[cust.ns_id][a]
            nxt_act = sum(nov_[nov_["Account Number"] == a]["net"], ZERO)
            res.rollforward.append(dict(customer=cust.name, ns_id=cust.ns_id, account=a, account_name=ACCOUNTS[a],
                                        opening=op, activity=act, gl_close=op + act, proposed=adj, corrected_close=op + act + adj,
                                        next_activity=nxt_act, after_next=op + act + adj + nxt_act))
        # tie: corrected 1150 at month end should equal this month's expected net usage
        rf1150 = [r for r in res.rollforward if r["ns_id"] == cust.ns_id and r["account"] == "1150"][0]
        rf1150["expected"] = res.usage[cust.ns_id]["net"]
        rf1150["ties"] = rf1150["corrected_close"] == rf1150["expected"]

    for j in res.journals:
        j["posting_period"] = plabel

    # ------------------------------------------------------------- priority + sort
    for f in res.findings:
        f.priority = _priority(f)
    order = {"High": 0, "Medium": 1, "Low": 2}
    cls_order = {ERROR: 0, NEEDS_INFO: 1, FOLLOW_UP: 2, VALID: 3}
    res.findings.sort(key=lambda f: (order[f.priority], cls_order[f.classification], -abs(f.revenue_effect) - abs(f.billing_effect)))
    # renumber in priority order so F01 is the most important item; keep every cross-reference in step
    remap = {f.id: f"F{i:02d}" for i, f in enumerate(res.findings, 1)}
    for f in res.findings:
        f.id = remap[f.id]
    for j in res.journals:
        j["finding"] = remap.get(j["finding"], j["finding"])
        j["je_id"] = "PJE-" + j["finding"] + ("-CA" if j["je_id"].endswith("-CA") else "")
    for d in res.billing_docs + res.plan_updates:
        d["finding"] = remap.get(d["finding"], d["finding"])

    # ------------------------------------------------------------- controls matrix
    res.controls = build_controls(res, m0, m1)
    return res


CONTROL_DEFS = [
    ("Contract terms on file", "Every customer has an executed order form in the contract repository."),
    ("Amendments reflected in billing", "Stripe invoices for the month use the terms of every amendment in effect."),
    ("Billed rate matches contract rate", "Unit price on every usage invoice line equals the contract overage rate."),
    ("Billed quantity matches calculated excess", "Runs billed equal de-duplicated usage minus allowance."),
    ("Usage invoice completeness", "Every customer with overage > 0 has a usage invoice; none without overage."),
    ("Usage accrual equals recalculated usage", "GL 4010 for the month equals the contract recalculation (net of approved concessions)."),
    ("Late-ingested usage", "No usage record was ingested after the usage invoice was created."),
    ("Platform invoices match contract schedule", "Platform invoices covering the month match the contract amount and billing period."),
    ("Platform recognition = contract days/365", "GL 4000 equals annual consideration × service days / 365, by layer."),
    ("Revenue plan includes all amendments", "Every executed amendment changing platform price is in the revenue plan sources."),
    ("Contract asset reclass correct", "1160 equals any net debit in 2200 after corrections."),
    ("Usage mapped to verified customer", "Customer has a verified product account; no unverified usage attributed."),
]


def build_controls(res: ReviewResult, m0: date, m1: date) -> List[dict]:
    out = []
    for cust in res.customers:
        c = res.contracts.get(cust.sf_id)
        u = res.usage.get(cust.ns_id, {})
        p = res.platform.get(cust.ns_id, {})
        row = {"Customer": cust.name}
        ok = lambda b: "✅ Pass" if b else "❌ Fail"
        na = "— n/a"
        row["Contract terms on file"] = ok(c is not None)
        if c is None:
            out.append(row)
            continue
        am = [a for a in c.amendments if a.effective_from < m1]
        stx = {a.billing_status for a in am}
        row["Amendments reflected in billing"] = ("❌ Fail" if "Not reflected" in stx else "🟠 Corrected" if "Corrected" in stx
                                                  else "✅ Pass") if am else na
        bl = u.get("billed_lines")
        blm = bl[bl["proration"].str.lower() != "true"] if bl is not None and len(bl) else None
        fixed = u.get("billed_net") == u.get("net")   # original line wrong but a supplemental/credit corrected it
        tri = lambda good: "✅ Pass" if good else ("🟠 Corrected" if fixed else "❌ Fail")
        row["Billed rate matches contract rate"] = tri(all(blm["unit_usd"] == u["rate"])) if blm is not None and len(blm) else na
        row["Billed quantity matches calculated excess"] = tri(u["billed_qty"] == u["excess"]) if blm is not None and len(blm) else na
        row["Usage invoice completeness"] = ok((u["excess"] > 0) == (bl is not None and len(bl) > 0)) if u.get("product_accounts") else na
        row["Usage accrual equals recalculated usage"] = ok(u["accrued"] == u["net"]) if u.get("product_accounts") else na
        row["Late-ingested usage"] = tri(len(u["late"]) == 0) if blm is not None and len(blm) else na
        row["Platform invoices match contract schedule"] = ok(all(r["expected"] == r["actual"] for r in p.get("rows", [])))
        row["Platform recognition = contract days/365"] = ok(p.get("expected_rev") == p.get("gl_rev"))
        pa = [a for a in c.amendments if a.has("platform_annual_fee") and a.effective_from < m1]
        row["Revenue plan includes all amendments"] = ok(all(a.document_id in p.get("plan_docs", []) for a in pa)) if pa else na
        rf = [r for r in res.rollforward if r["ns_id"] == cust.ns_id and r["account"] == "1160"]
        row["Contract asset reclass correct"] = ok(rf[0]["proposed"] == 0) if rf else na
        row["Usage mapped to verified customer"] = ok(bool(u.get("product_accounts")))
        out.append(row)
    return out
