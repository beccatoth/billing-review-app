"""What files the review needs, where they come from, and how to read them.

This catalogue drives the 'Load files' page: every slot shows its plain-English
description, the system it is exported from, the date range to include and the
columns that must be present.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd


@dataclass
class Source:
    key: str
    filename: str
    group: str
    label: str
    contains: str
    export_from: str
    date_range: str
    columns: List[str]
    required: bool = True
    expected_rows_default: Optional[int] = None


SOURCES: List[Source] = [
    # ----------------------------------------------------------- Usage
    Source("usage", "product_usage.csv", "1. Usage (metering)", "Daily product usage",
           "One row per product account per day: workflow runs used. Duplicates are expected and handled.",
           "Metering system daily usage export",
           "Service month under review AND the prior month (so the prior-month accrual can be checked).",
           ["record_id", "product_account_id", "sku", "service_date", "units", "ingested_at",
            "record_status", "replaces_record_id", "batch_id"]),
    Source("batch_controls", "usage_batch_controls.csv", "1. Usage (metering)", "Usage batch control totals",
           "Independent record counts and unit totals per daily batch, used to prove the usage file is complete.",
           "Metering system control report",
           "Same dates as the usage file.",
           ["service_date", "batch_id", "distinct_active_record_count", "active_units_total", "extracted_at"],
           expected_rows_default=61),
    # ----------------------------------------------------------- Stripe
    Source("invoices", "stripe_invoices.csv", "2. Billing (Stripe)", "Stripe invoices (headers)",
           "Every invoice issued, with status and totals in cents.",
           "Stripe Sigma / custom invoice report",
           "All invoices issued from the start of the prior month THROUGH the close cutoff — "
           "this must include the usage invoices dated the 1st of the following month.",
           ["id", "number", "customer_id", "status", "created", "finalized_at", "currency", "subtotal",
            "total_discount_amount", "total", "source_system"], expected_rows_default=124),
    Source("invoice_lines", "stripe_invoice_lines.csv", "2. Billing (Stripe)", "Stripe invoice lines",
           "Each invoice line: product, price ID, quantity, unit price (decimal cents) and service period.",
           "Stripe Sigma / custom invoice line report",
           "Lines for every invoice in the invoice file.",
           ["id", "invoice_id", "description", "product_id", "price_id", "quantity", "unit_amount_decimal",
            "amount", "discount_amount", "period_start", "period_end", "proration"], expected_rows_default=124),
    Source("credit_notes", "stripe_credit_notes.csv", "2. Billing (Stripe)", "Stripe credit notes",
           "Credit notes with status (issued or void). Only issued notes have an effect.",
           "Stripe credit notes export",
           "All credit notes created through the close cutoff.",
           ["id", "number", "invoice_id", "customer_id", "status", "created", "voided_at", "currency",
            "amount", "reason"], expected_rows_default=2),
    Source("credit_note_lines", "stripe_credit_note_lines.csv", "2. Billing (Stripe)", "Stripe credit note lines",
           "Credit note lines linked to the original invoice line and service period.",
           "Stripe credit note lines export",
           "Lines for every credit note in the credit note file.",
           ["id", "credit_note_id", "invoice_line_item_id", "description", "amount", "quantity",
            "service_start", "service_end_exclusive", "service_period_source"], expected_rows_default=2),
    # ----------------------------------------------------------- NetSuite
    Source("opening", "netsuite_opening_balances.csv", "3. Accounting (NetSuite)", "Opening balances",
           "Prior month-end balance for accounts 1100, 1150, 1160 and 2200 for every customer.",
           "NetSuite saved search (customer balances by account)",
           "As of the last day of the prior month.",
           ["Customer Internal ID", "Account Number", "Account Name", "As Of Date", "Debit Balance",
            "Credit Balance", "Source Reference"], expected_rows_default=120),
    Source("gl", "netsuite_gl_detail.csv", "3. Accounting (NetSuite)", "GL posting detail",
           "Every posting line to accounts 1100, 1150, 1160, 2200, 4000 and 4010.",
           "NetSuite saved search (GL detail)",
           "Review-month posting period plus next-month postings created through the cutoff.",
           ["Internal ID", "Line ID", "Transaction Type", "Document Number", "Date", "Posting Period",
            "Account Number", "Customer Internal ID", "Debit", "Credit", "Memo", "External Source ID",
            "Revenue Plan ID", "Created At"], expected_rows_default=340),
    Source("rev_plan", "netsuite_revenue_plan_detail.csv", "3. Accounting (NetSuite)", "Revenue plan detail",
           "Platform revenue recognition schedule by customer and month.",
           "NetSuite ARM revenue plan detail",
           "All plan periods for active plans.",
           ["Revenue Plan ID", "Revenue Element ID", "Customer Internal ID", "Source Document ID", "Plan Type",
            "Recognition Start", "Recognition End Exclusive", "Posting Period", "Planned Amount",
            "Recognized Amount", "Journal Internal ID", "Plan Updated At"], expected_rows_default=362),
    # ----------------------------------------------------------- Reference
    Source("accounts", "salesforce_accounts.csv", "4. Customers & contracts", "Salesforce accounts",
           "Customer list with the Stripe and NetSuite IDs used to connect the systems.",
           "Salesforce account report",
           "All active customers.",
           ["AccountId", "AccountName", "ParentId", "BillingCountry", "StripeCustomerId__c",
            "NetSuiteCustomerInternalId__c", "AccountOwner"], expected_rows_default=30),
    Source("mapping", "product_account_mapping.csv", "4. Customers & contracts", "Product account mapping",
           "Which product workspaces belong to which customer. Blank customer = unverified owner.",
           "Product Operations account registry",
           "All product accounts, current state.",
           ["product_account_id", "salesforce_account_id", "account_label", "effective_from", "effective_to",
            "mapping_updated_at"], expected_rows_default=32),
]

SOURCE_BY_KEY: Dict[str, Source] = {s.key: s for s in SOURCES}

# Usage unique-record expectation is separate from physical rows (duplicates allowed).
EXPECTED_EXTRA = {"usage_unique_records": 1924}


def read_table(data, filename: str) -> pd.DataFrame:
    """Read CSV or Excel into all-string columns; blanks stay blank (never zero)."""
    name = filename.lower()
    if hasattr(data, "read"):
        raw = data.read()
    else:
        raw = data
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        df = pd.read_excel(io.BytesIO(raw), dtype=str, keep_default_na=False)
    else:
        df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df.columns = [str(c).strip() for c in df.columns]
    return df.fillna("")


def check_columns(key: str, df: pd.DataFrame) -> List[str]:
    """Return plain-English problems with a table's columns (empty list = OK)."""
    src = SOURCE_BY_KEY[key]
    missing = [c for c in src.columns if c not in df.columns]
    if missing:
        return [f"{src.filename}: missing column(s) {', '.join(missing)}. "
                f"Re-export from {src.export_from} including these columns."]
    if len(df) == 0:
        return [f"{src.filename}: the file has no rows."]
    return []
