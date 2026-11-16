"""Turn the Sales & Billing messages PDF into a starter approvals log.

The suggestions come from simple keyword rules and are shown to the reviewer to confirm or
edit on Contracts & Terms → Approvals log. Only rows with type 'usage_concession' and status
'approved' change any amount.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import List

import pandas as pd

APPROVAL_COLUMNS = ["reference", "date", "customer_name", "type", "amount", "service_month", "status", "effect",
                    "summary", "source"]

_HEADER = re.compile(r"(?m)^(MSG-\d{4}-\d+) \| ([A-Z][a-z]+ \d{1,2}, \d{4}) [\d:]+ [A-Za-z/_]+ \| ")


def parse_messages(text: str, customer_names: List[str]) -> pd.DataFrame:
    t = re.sub(r"RelayCloud \| Fictional challenge material \| Page \d+ of \d+", "", text)
    heads = list(_HEADER.finditer(t))
    rows = []
    short = {n: n.split(",")[0].strip() for n in customer_names}
    for i, h in enumerate(heads):
        body = t[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(t)]
        ref, dt = h.group(1), datetime.strptime(h.group(2), "%B %d, %Y").date()
        full = re.sub(r"\s+", " ", body).strip()
        # drop the "From -> To |" routing prefix; keep subject + message
        full = full.split(" | ", 1)[1] if " | " in full[:200] else full
        low = full.lower()
        custs = [n for n, s in short.items() if s.lower() in low]
        amt = re.search(r"USD ([\d,]+(?:\.\d+)?)", full)
        month = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\b", full)
        svc = ""
        if month:
            m = datetime.strptime(month.group(1), "%B").month
            y = dt.year if m <= dt.month else dt.year - 1
            svc = f"{y:04d}-{m:02d}"
        typ, status, effect = "other", "informational", "None"
        if "concession" in low or ("off" in low and "usage" in low and amt):
            typ = "usage_concession"
            if "approved:" in low or low.startswith("approved"):
                status, effect = "approved", "Reduces usage consideration for the service month"
            else:
                status, effect = "requested", "None – request only until approved"
        elif "void" in low and "credit" in low:
            typ, effect = "credit_void", "None – void credit note"
        elif "workspace" in low and ("verify" in low or "verified" in low):
            typ, status, effect = "workspace_ownership", "requested", "None until Product Operations verifies owner"
            custs = []
        elif "amendment" in low and ("signed" in low or "executed" in low):
            typ, effect = "amendment_notice", "None – use executed documents"
        rows.append(dict(reference=ref, date=dt.isoformat(), customer_name=" / ".join(custs), type=typ,
                         amount=(amt.group(1).replace(",", "") if amt and typ in ("usage_concession", "credit_void") else ""),
                         service_month=svc, status=status, effect=effect, summary=full[:400],
                         source="Suggested from messages – confirm"))
    df = pd.DataFrame(rows, columns=APPROVAL_COLUMNS)
    # a request that a later message approved is superseded, not still open
    appr = df[(df["type"] == "usage_concession") & (df["status"] == "approved")]
    for i, r in df[(df["type"] == "usage_concession") & (df["status"] == "requested")].iterrows():
        if ((appr["customer_name"] == r["customer_name"]) & (appr["service_month"] == r["service_month"])).any():
            df.at[i, "status"] = "informational"
            df.at[i, "effect"] = "None – superseded by the approval"
    return df
