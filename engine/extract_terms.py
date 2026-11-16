"""Deterministic extraction of commercial terms from executed order form / amendment PDFs.

No AI service is called: the PDF text is read with pypdf and matched with simple,
reviewable patterns. Every extracted value is shown next to the PDF text in the
app (Contracts & Terms), where it can be corrected.
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Dict, Optional

from pypdf import PdfReader

TERMS_COLUMNS = [
    "document_id", "contract_id", "customer_name", "document_type", "effective_from",
    "service_start", "service_end_inclusive", "billing_frequency", "billing_anchor",
    "platform_annual_fee", "platform_invoice_amount", "invoice_amount_effective_from",
    "included_runs_per_month", "overage_rate_per_run", "catch_up_charge",
    "incremental_consideration", "executed_date", "billing_loaded_at", "source_excerpt", "term_source", "notes",
]

FIELD_HELP = {
    "effective_from": "Date the document's terms start to apply.",
    "service_start": "First day of service (order forms only).",
    "service_end_inclusive": "Last day of service, inclusive (as printed on the order form).",
    "billing_frequency": "Monthly or Annual platform invoicing, in advance.",
    "billing_anchor": "Day of month the platform invoice is issued.",
    "platform_annual_fee": "Annual platform consideration in USD (amendments: the new annual rate).",
    "platform_invoice_amount": "Amount of each platform invoice in USD.",
    "invoice_amount_effective_from": "Amendments: first invoice date at the new invoice amount.",
    "included_runs_per_month": "Workflow runs included each calendar month.",
    "overage_rate_per_run": "USD charged per run above the allowance.",
    "catch_up_charge": "Amendments: one-time additional charge for service already invoiced at the old price.",
    "incremental_consideration": "Amendments: total additional platform consideration over the remaining term.",
}

_DATE = r"([A-Z][a-z]+ \d{1,2}, \d{4})"


def _num(s: Optional[str]) -> str:
    return s.replace(",", "") if s else ""


def _iso(s: Optional[str]) -> str:
    if not s:
        return ""
    return datetime.strptime(s.strip(), "%B %d, %Y").date().isoformat()


def pdf_text(data) -> str:
    raw = data.read() if hasattr(data, "read") else data
    reader = PdfReader(io.BytesIO(raw))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def extract_from_text(text: str) -> Dict[str, str]:
    t = re.sub(r"[ \t]+", " ", text)
    out = {c: "" for c in TERMS_COLUMNS}
    m = re.search(r"EXECUTED (ORDER FORM|AMENDMENT)\s*\n\s*([A-Z0-9\-]+)", t)
    if m:
        out["document_type"] = "order_form" if m.group(1) == "ORDER FORM" else "amendment"
        out["document_id"] = m.group(2)
    m = re.search(r"Executed ([A-Z][a-z]+ \d{1,2}, \d{4})", t)
    out["executed_date"] = _iso(m.group(1)) if m else ""
    m = re.search(r"Contract (CT-\d+)", t)
    out["contract_id"] = m.group(1) if m else ""
    m = re.search(r"CUSTOMER / BILL TO SUPPLIER\s*\n(.+?) RelayCloud, Inc\.", t)
    out["customer_name"] = m.group(1).strip() if m else ""

    excerpts = []
    if out["document_type"] == "order_form":
        m = re.search(r"SERVICE START SERVICE END BILLING PAYMENT / CURRENCY\s*\n" + _DATE + " " + _DATE +
                      r" (Monthly|Annual) in advance", t)
        if m:
            out["service_start"] = _iso(m.group(1))
            out["effective_from"] = out["service_start"]
            out["service_end_inclusive"] = _iso(m.group(2))
            out["billing_frequency"] = m.group(3)
            excerpts.append(m.group(0).split("\n")[-1])
        m = re.search(r"in advance \$([\d,]+\.\d{2}) / (month|year)", t)
        if m:
            amt = _num(m.group(1))
            out["platform_invoice_amount"] = amt
            if m.group(2) == "year":
                out["platform_annual_fee"] = amt
            excerpts.append("Platform: " + m.group(0))
        m = re.search(r"ANNUAL PLATFORM COMMITMENT \$([\d,]+\.\d{2})", t)
        if m:
            out["platform_annual_fee"] = _num(m.group(1))
            excerpts.append(m.group(0))
        m = re.search(r"([\d,]+) included runs per calendar month", t)
        if m:
            out["included_runs_per_month"] = _num(m.group(1))
            excerpts.append("Workflow runs: " + m.group(0))
        m = re.search(r"\$([\d\.]+) / excess run", t)
        if m:
            out["overage_rate_per_run"] = m.group(1)
            excerpts.append("Overage: " + m.group(0))
        m = re.search(r"Platform fees are (invoiced [^.]+)\.", t.replace("\n", " "))
        if m:
            excerpts.append("Billing: Platform fees are " + m.group(1) + ".")
            a = re.search(r"on the (\d+)(?:st|nd|rd|th) of each month", m.group(1))
            out["billing_anchor"] = a.group(1) if a else (out["service_start"][-2:].lstrip("0") if out["service_start"] else "")
    else:
        m = re.search(r"ORIGINAL ORDER EFFECTIVE DATE TERM END \(INCLUSIVE\)\s*\n(\S+) " + _DATE + " " + _DATE, t)
        if m:
            out["effective_from"] = _iso(m.group(2))
            excerpts.append(f"Amends {m.group(1)}, effective {m.group(2)}")
        flat = t.replace("\n", " ")
        m = re.search(r"Included workflow runs ([\d,]+) / calendar month ([\d,]+) / calendar month", flat)
        if m:
            out["included_runs_per_month"] = _num(m.group(2))
            excerpts.append(m.group(0))
        m = re.search(r"Overage rate \$([\d\.]+) / run \$([\d\.]+) / run( \(unchanged\))?", flat)
        if m and not m.group(3):
            out["overage_rate_per_run"] = m.group(2)
            excerpts.append(m.group(0))
        m = re.search(r"Annual platform rate \$([\d,]+\.\d{2}) / year \$([\d,]+\.\d{2}) / year", flat)
        if m:
            out["platform_annual_fee"] = _num(m.group(2))
            excerpts.append(m.group(0))
        m = re.search(r"Monthly platform invoice \$([\d,]+\.\d{2}) / month \$([\d,]+\.\d{2}) from ([A-Z][a-z]{2}) (\d+)", flat)
        if m:
            out["platform_invoice_amount"] = _num(m.group(2))
            y = re.search(r"from ([A-Z][a-z]+) (\d{4}) through", flat)
            if y:
                out["invoice_amount_effective_from"] = datetime.strptime(
                    f"{y.group(1)} {m.group(4)}, {y.group(2)}", "%B %d, %Y").date().isoformat()
            excerpts.append(m.group(0))
        m = re.search(r"catch-up Not previously billed \$([\d,]+\.\d{2})", flat)
        if m:
            out["catch_up_charge"] = _num(m.group(1))
            c = re.search(r"(October|[A-Z][a-z]+) upgrade catch-up: USD [\d,\.]+, using [^.]+\.\d+ ?[^.]*\.", flat)
            excerpts.append(c.group(0) if c else m.group(0))
        m = re.search(r"Incremental consideration for (\d+) remaining service days is USD ([\d,]+\.\d{2})", flat)
        if m:
            out["incremental_consideration"] = _num(m.group(2))
            excerpts.append(m.group(0))
        m = re.search(r"(This amendment changes only [^.]+\.)", flat)
        if m:
            excerpts.append(m.group(1))
    out["source_excerpt"] = " | ".join(excerpts)
    out["term_source"] = "Extracted from PDF"
    return out


def extract_terms(data) -> Dict[str, str]:
    return extract_from_text(pdf_text(data))
