# RelayCloud Billing Review

This is a monthly workflow for revenue accountants. For every customer it checks:

- **what they agreed to pay** (executed contracts),
- **what they used** (metering),
- **what they were billed** (Stripe), and
- **what Accounting recorded** (NetSuite).

It then explains each difference, classifies it, and proposes the correction. Billing corrections and October revenue effects are kept separate.

Built for the Numeric Finance Engineer Cup II: Billing Battle, Round 1. All data is fictional.

---

## For accountants: running the monthly close (no coding)

The app opens with the **prior close already loaded**: the September 2026 close, with 30 customers, 32 product workspaces, the terms of all 30 order forms, and each customer's September results (usage, billing, revenue and September 30 balances). Customer Summary and Customer Detail show September before you load anything. Once October is run, you can compare the two months side by side. You only add the month's files.

1. **Open the app.** It opens on Contracts & Terms. Use the hosted link, or double-click `run.command` (Mac) or `run.bat` (Windows).
2. **File Upload.** Set the service month and close cutoff, then drag in the month's files: the usage, Stripe and NetSuite exports and `billing_followups.pdf` (approvals). Each file gets a ✅ as it loads.
   - **Contracts & Terms → Load new contracts** takes new contract PDFs (for October, the two amendments) plus any updated Salesforce accounts or product account mapping. The app is the contract repository; dropping the contract register alongside new documents records their billing-load dates once.
   - The checklist shows which system each file comes from, which dates it must cover, and whether it passed its checks.
   - To use the challenge data, drop the files from `data/sample_oct_2026/` (CSVs and `billing_followups.pdf` on File Upload; `contracts/AM-*.pdf` on Contracts & Terms).
   - Missing files are listed only if you click **Run review** before everything is loaded.
3. **Run review**, then work through the sidebar:

| Section | Page | Answers |
|---|---|---|
| Customers & Contracts | Contracts & Terms | Every contract's terms in one table; click a document ID to view the executed PDF. Shows the contract ID and the date each document was loaded to billing – recorded once from the contract register when the document is added, and kept with the repository. New contracts and master files are added on *Load new contracts*. |
| | Customer Detail | **Customer Data** (contracts & entitlements with links to the executed PDFs, system IDs, product accounts, Stripe products), **Monthly Summary** (usage, platform, invoices and month-end balances by GL account for the selected month) and **Customer Trends** (prior month vs. current month). |
| Close Review | File Upload | Loads this month's files on top of the prior close. After **Run review**: result counts and the **controls matrix** (12 automated checks per customer – **Q3: how do we catch it next month?**). |
| | Findings and Next Steps | **Q2: What needs action?** Errors, needs information, follow-ups and valid explanations. **Findings** tab: each finding shows a variance table (Metric · Expected · Actual · Variance · Source), the cause, the billing and October-books actions, and links to the contract PDFs and Customer Detail. **Next Steps** tab: proposed journal entries, proposed billing, billing vs. revenue and revenue plan updates. |
| | Data Quality | Can we trust the files? Warnings first, then info, then passes: duplicates removed, usage tie-out to batch controls, cutoff, GL integrity. |
| | Customer Summary | **Q1: What should we have billed?** Expected vs. actual for each customer, platform and usage shown separately. Toggle between the current month, the prior close and the change. |
| | Recommended Actions | Balance roll-forward and the **next-month checklist**. |
| | Assumptions & Open Items | Open items, judgment calls, approvals used and the accounting basis. |

4. **Download results (Excel)** from the sidebar.
5. **Save as next month's starting point** from the sidebar. This saves the customers, workspaces and every contract's terms, including this month's amendments. Next month, drop that `close_baseline_….xlsx` on File Upload along with the new month's files.

**Next month** you repeat steps 2–5 with the new files; nothing needs rebuilding.

---

## October 2026 results (supplied data)

The full write-up is in `results/OCTOBER_2026_CLOSE_SUMMARY.md` and the workbook is `results/billing_review_2026_10.xlsx`.

| Finding | Customer | Billing effect | Oct revenue effect | Action |
|---|---|---:|---:|---|
| F01 Usage billed at 5¢ vs contract 16¢ (Stripe price changed) | Juniper Signal Works | +$3,135.22 | +$3,135.22 | Supplemental invoice + Oct JE |
| F02 Allowance amendment never loaded to billing | Cedar Harbor Labs | −$3,096.80 | −$3,096.80 | Linked credit note + Oct JE |
| F03 Overage never invoiced or accrued | Yarrow Ridge | +$3,074.28 | +$3,074.28 | Usage invoice + Oct JE |
| F04 Platform upgrade not billed or recognised | Meridian Process Systems | +$526.03 | +$526.03 | Catch-up invoice + recognition JE + incremental contract-asset reclass |
| F05 Late-ingested usage missed by invoice | Beacon Marsh | +$100.00 | $0.00 | Supplemental invoice only (accrual already right) |

**Totals:**
- Under-billed $6,835.53 and over-billed $3,096.80.
- October revenue understated $6,735.53 and overstated $3,096.80, for a net effect of **+$3,638.73**.

The following are kept out of the totals:
- Unverified workspace exposure (between $390.48 and $19,904.80 depending on the owner's contract; $0 if internal).
- Willow Passage's unapproved $250 concession request.
- Meridian's November invoice at the old price (a later-service follow-up).

---

## For IT / judges: run, test, deploy

```bash
# local
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py              # opens http://localhost:8501

# tests (golden results to the cent + rerun-after-corrections)
.venv/bin/python -m pytest -q tests

# regenerate the prior-close baseline, the rerun demo packet and the results/ folder
.venv/bin/python tools/make_baseline.py
.venv/bin/python tools/make_rerun_demo.py
.venv/bin/python tools/export_results.py
```

**Deploy to Streamlit Community Cloud (free):**
1. Push this folder to a GitHub repository.
2. Sign in at share.streamlit.io and click **Create app**.
3. Choose the repo and branch, set main file `app.py` and Python 3.11+, then click **Deploy**.

Vercel can't host Streamlit apps.

### How it's built

| Piece | File | What it does |
|---|---|---|
| Close snapshot | `engine/snapshot.py` | Per-customer results of a close; September is rebuilt from the supplied files, and later months are saved with the next month's starting point. |
| Close packets | `engine/packet.py` | The prior-close starting point (`data/baseline_sep_2026/`) plus the month's files; saves the next month's starting point. |
| Source catalogue | `engine/sources.py` | Every expected file: description, export source, date range, required columns. Drives the Load page. |
| Term extraction | `engine/extract_terms.py` | Reads executed PDFs with fixed text patterns (no AI service). Values are shown next to the PDF text and can be corrected. |
| Messages | `engine/messages.py` | Reads `billing_followups.pdf` into the approvals log (approved concessions vs. requests). |
| Effective-dated terms | `engine/terms.py` | Order form plus amendments. An amendment changes only the terms it names. Platform consideration is split into a base layer and an upgrade layer. |
| Data quality | `engine/validate.py` | Row counts, de-duplication on `record_id`, batch-control tie-out, mapping coverage, cross-system IDs, Stripe header/line tie, cutoff, GL balance, GL↔Stripe, opening balances. |
| Review | `engine/review.py` | Expected vs. billed vs. booked; root-cause rules; findings; proposed JEs and billing documents; roll-forward; controls matrix. |
| Reports | `engine/report.py` | Tables and the Excel workbook. |
| UI | `app.py` | Streamlit pages. |
| Tests | `tests/test_golden.py` | 20 tests reproduce every amount to the cent and cover the monthly flow. |

Money uses Python `Decimal`, rounding half up once at the points START_HERE specifies.

**Rerun demo:** the folder `data/rerun_demo_after_corrections/` contains the same October packet re-exported *after* the proposed corrections were booked. Dropping those files on File Upload (with the cutoff set to Nov 14, 2026 18:00) and running again produces 5 resolved, 0 new, and 4 still open:
- The original Juniper, Cedar and Beacon invoice lines show **🟠 Corrected** in the controls matrix.
- The open items stay open; nothing is assumed resolved.

---

## Walkthrough script (3–5 min)

1. **Rerun, part 1** (0:00–0:45). File Upload: the September close is already loaded. Drop October's files and the two amendment PDFs, the checklist turns green, click Run review. On Data Quality, show the duplicate removed and all 61 batches tying.
2. **Important finding: Cedar Harbor** (0:45–2:15).
   - Findings → F02.
   - Contracts & Terms → click AM-OF-2026-157, shown next to the PDF text: 30,000 → 45,000 from Oct 16. Customer detail shows it was never loaded to billing.
   - Customer detail → allowance (30,000×15 + 45,000×16)/31 = 37,742 → 20,728 excess × $0.40 = $8,291.20, vs. $11,388 invoiced and accrued.
   - Findings and Next Steps → Next Steps → PJE-F02 Dr 4010 / Cr 1150 $3,096.80, plus the November linked credit note, which is Dr 1150 / Cr 1100 and not revenue again.
3. **Judgment call: unverified workspace** (2:15–3:15). F07: 49,762 runs with no verified owner. Willow Passage has no workspace and asked for a concession, but per MSG-2026-131/132 neither fact counts as evidence.
   - The workspace is not assigned to anyone.
   - Exposure is shown as a range across every contract and kept out of totals.
   - Product Ops is asked for ownership evidence.
4. **Rerun, part 2** (3:15–4:15). Set the cutoff to Nov 14, drop the files from `data/rerun_demo_after_corrections/` and click **Run review**. The result is 5 resolved, 0 new, and 4 open. The controls matrix under Run review shows 🟠 Corrected, which proves each correction landed exactly once.
5. **Close** (4:15–4:45). Download the Excel results.
