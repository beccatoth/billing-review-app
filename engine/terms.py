"""Effective-dated contract terms.

An order form sets every term. An amendment changes only the terms it names
(non-blank cells); everything else carries forward (START_HERE: source precedence).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .util import D, ZERO, add_months, is_blank, parse_date, parse_ts


@dataclass
class Doc:
    document_id: str
    document_type: str
    effective_from: date
    row: Dict[str, str]
    # Set by the review from Stripe evidence: "Reflected" / "Not reflected" / "Corrected" / "Not yet billed"
    billing_status: str = ""
    billing_evidence: str = ""

    def get(self, key) -> str:
        v = self.row.get(key, "")
        return "" if is_blank(v) else str(v).strip()

    def has(self, key) -> bool:
        return self.get(key) != ""

    @property
    def executed(self) -> Optional[date]:
        return parse_date(self.get("executed_date"))


@dataclass
class ContractTerms:
    customer_sf_id: str
    contract_id: str
    order: Doc
    amendments: List[Doc] = field(default_factory=list)

    # ---- basic
    @property
    def service_start(self) -> date:
        return parse_date(self.order.get("service_start"))

    @property
    def service_end_excl(self) -> date:
        return parse_date(self.order.get("service_end_inclusive")) + timedelta(days=1)

    @property
    def billing_frequency(self) -> str:
        return self.order.get("billing_frequency") or "Monthly"

    @property
    def documents(self) -> List[Doc]:
        return [self.order] + self.amendments

    def _value_on(self, key: str, day: date) -> Tuple[str, Doc]:
        val, src = self.order.get(key), self.order
        for a in self.amendments:
            if a.has(key) and a.effective_from <= day:
                val, src = a.get(key), a
        return val, src

    def allowance_on(self, day: date) -> Tuple[int, Doc]:
        v, src = self._value_on("included_runs_per_month", day)
        return int(D(v)), src

    def rate_on(self, day: date) -> Tuple[Decimal, Doc]:
        v, src = self._value_on("overage_rate_per_run", day)
        return D(v), src

    # ---- platform consideration layers (base + each upgrade increment)
    def platform_layers(self) -> List[dict]:
        """Each layer is recognised separately so the base schedule is preserved."""
        layers = []
        base_annual = D(self.order.get("platform_annual_fee"))
        layers.append(dict(doc=self.order, label=f"Base ({self.order.document_id})", annual=base_annual,
                           start=self.service_start, end_excl=self.service_end_excl, total=None))
        prev = base_annual
        for a in self.amendments:
            if a.has("platform_annual_fee"):
                new = D(a.get("platform_annual_fee"))
                inc = new - prev
                if inc != 0:
                    total = D(a.get("incremental_consideration")) if a.has("incremental_consideration") else None
                    layers.append(dict(doc=a, label=f"Upgrade increment ({a.document_id})", annual=inc,
                                       start=a.effective_from, end_excl=self.service_end_excl, total=total))
                prev = new
        return layers

    # ---- platform invoice schedule
    def platform_invoice_schedule(self) -> List[dict]:
        out = []
        start, end = self.service_start, self.service_end_excl
        if self.billing_frequency.lower().startswith("annual"):
            out.append(dict(period_start=start, period_end=add_months(start, 12), invoice_date=start,
                            amount=D(self.order.get("platform_invoice_amount") or self.order.get("platform_annual_fee")),
                            kind="Annual platform invoice", source=self.order.document_id))
        else:
            k = 0
            while True:
                ps = add_months(start, k)
                if ps >= end:
                    break
                pe = min(add_months(start, k + 1), end)
                amt, src = D(self.order.get("platform_invoice_amount")), self.order.document_id
                for a in self.amendments:
                    eff = parse_date(a.get("invoice_amount_effective_from")) if a.has("invoice_amount_effective_from") else None
                    if a.has("platform_invoice_amount") and eff and ps >= eff:
                        amt, src = D(a.get("platform_invoice_amount")), a.document_id
                out.append(dict(period_start=ps, period_end=pe, invoice_date=ps, amount=amt,
                                kind="Monthly platform invoice", source=src))
                k += 1
        # one-time catch-up charges named by an amendment
        for a in self.amendments:
            if a.has("catch_up_charge"):
                nxt = min((s["period_start"] for s in out if s["period_start"] > a.effective_from), default=self.service_end_excl)
                out.append(dict(period_start=a.effective_from, period_end=nxt, invoice_date=None,
                                amount=D(a.get("catch_up_charge")), kind="Upgrade catch-up (one-time)",
                                source=a.document_id))
        return out


def _norm(name: str) -> str:
    return " ".join(str(name).lower().replace(",", " ").replace(".", " ").split())


def build_contracts(terms_df: pd.DataFrame, accounts_df: pd.DataFrame) -> Tuple[Dict[str, ContractTerms], List[str]]:
    """Build contracts from the contract documents themselves (the app is the contract repository).

    Order forms are linked to a Salesforce account by the customer name on the document (fallback: the contract
    ID suffix equals the NetSuite customer ID). Amendments attach to the order form with the same contract ID.
    Returns contracts keyed by Salesforce account ID, plus problems.
    """
    problems: List[str] = []
    accts = accounts_df.fillna("").to_dict("records") if accounts_df is not None else []
    by_name = {_norm(a["AccountName"]): a["AccountId"] for a in accts}
    by_ns = {str(a["NetSuiteCustomerInternalId__c"]): a["AccountId"] for a in accts}
    docs = [r for r in terms_df.fillna("").to_dict("records") if str(r.get("document_id", "")).strip()]
    contracts: Dict[str, ContractTerms] = {}
    sf_by_contract: Dict[str, str] = {}
    for r in sorted(docs, key=lambda x: (x.get("document_type") != "order_form", str(x.get("effective_from")))):
        doc_id = r["document_id"].strip()
        eff = parse_date(r.get("effective_from"))
        if eff is None:
            problems.append(f"{doc_id} · no effective date on the document")
            continue
        doc = Doc(doc_id, r.get("document_type") or "order_form", eff, r)
        cid = str(r.get("contract_id", "")).strip()
        if doc.document_type == "order_form":
            sf = by_name.get(_norm(r.get("customer_name", ""))) or by_ns.get(cid.split("-")[-1] if cid else "")
            if not sf:
                problems.append(f"{doc_id} · customer '{r.get('customer_name')}' not found in Salesforce accounts")
                continue
            contracts[sf] = ContractTerms(sf, cid, doc)
            sf_by_contract[cid] = sf
        else:
            sf = sf_by_contract.get(cid)
            if not sf:
                problems.append(f"{doc_id} · no order form on file for contract {cid or '(blank)'}")
                continue
            contracts[sf].amendments.append(doc)
    for c in contracts.values():
        c.amendments.sort(key=lambda d: d.effective_from)
    return contracts, problems
