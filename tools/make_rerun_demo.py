"""Build data/rerun_demo_after_corrections/ – the October 2026 packet as it would look AFTER the
Controller approves and the team books every correction this review proposes.

Used in the walkthrough to show a rerun: the same workflow, new files, and every error clears
(each correction appears exactly once). Run:  python tools/make_rerun_demo.py
"""
import csv
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "sample_oct_2026")
DST = os.path.join(ROOT, "data", "rerun_demo_after_corrections")
CUTOFF = "2026-11-14T18:00:00-07:00"
CREATED_JE = "2026-11-12T10:00:00-07:00"
ISSUED = "2026-11-13T09:00:00-07:00"


def rows(name):
    with open(os.path.join(SRC, name), newline="", encoding="utf-8") as fh:
        r = list(csv.DictReader(fh))
    return r, list(r[0].keys())


def write(name, data, cols):
    with open(os.path.join(DST, name), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(data)


def main():
    if os.path.exists(DST):
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST)
    acc, _ = rows("salesforce_accounts.csv")
    cus = {a["AccountName"].split(",")[0]: (a["StripeCustomerId__c"], a["NetSuiteCustomerInternalId__c"]) for a in acc}
    inv, inv_cols = rows("stripe_invoices.csv")
    lines, line_cols = rows("stripe_invoice_lines.csv")
    inv_by_num = {i["number"]: i for i in inv}
    line_by_inv = {l["invoice_id"]: l for l in lines}

    def add_invoice(key, who, desc, product, price, qty, unit, cents, ps, pe, proration):
        stripe_id, ns = cus[who]
        iid, num = f"in_demo{key}", f"RC-261113-{ns}-{key}"
        inv.append(dict(id=iid, number=num, customer_id=stripe_id, status="open", created=ISSUED, finalized_at=ISSUED,
                        currency="usd", subtotal=cents, total_discount_amount=0, total=cents, source_system="stripe"))
        lines.append(dict(id=f"il_demo{key}", invoice_id=iid, description=desc, product_id=product, price_id=price,
                          quantity=qty, unit_amount_decimal=unit, amount=cents, discount_amount=0, period_start=ps,
                          period_end=pe, proration=proration))
        return iid, num, ns

    oct0, nov0 = "2026-10-01T00:00:00-06:00", "2026-11-01T00:00:00-06:00"
    jun_line = line_by_inv[inv_by_num[f"RC-261101-{cus['Juniper Signal Works'][1]}-U"]["id"]]
    yar_price = line_by_inv[inv_by_num[f"RC-261001-{cus['Yarrow Ridge'][1]}-U"]["id"]]["price_id"]
    bea_line = line_by_inv[inv_by_num[f"RC-261101-{cus['Beacon Marsh'][1]}-U"]["id"]]
    mer_line = line_by_inv[inv_by_num[f"RC-261101-{cus['Meridian Process Systems'][1]}-P"]["id"]]
    new_docs = [
        add_invoice("SJ", "Juniper Signal Works", "Supplemental – October usage at contract rate (16¢ vs 5¢ billed)",
                    "prod_workflow_run", jun_line["price_id"], 28502, "11.00", 313522, oct0, nov0, "true"),
        add_invoice("UY", "Yarrow Ridge", "Workflow run overage", "prod_workflow_run", yar_price, 25619, "12.00",
                    307428, oct0, nov0, "false"),
        add_invoice("SB", "Beacon Marsh", "Supplemental – October usage ingested after invoice", "prod_workflow_run",
                    bea_line["price_id"], 1000, "10.00", 10000, oct0, nov0, "false"),
        add_invoice("CM", "Meridian Process Systems", "October upgrade catch-up (AM-OF-2026-164)", "prod_platform",
                    mer_line["price_id"], 1, "52603", 52603, "2026-10-16T00:00:00-06:00", nov0, "true"),
    ]
    write("stripe_invoices.csv", inv, inv_cols)
    write("stripe_invoice_lines.csv", lines, line_cols)

    cn, cn_cols = rows("stripe_credit_notes.csv")
    cnl, cnl_cols = rows("stripe_credit_note_lines.csv")
    ced_inv = inv_by_num[f"RC-261101-{cus['Cedar Harbor Labs'][1]}-U"]
    ced_line = line_by_inv[ced_inv["id"]]
    cn.append(dict(id="cn_demoCEDAR", number=f"CN-{cus['Cedar Harbor Labs'][1]}-20261113", invoice_id=ced_inv["id"],
                   customer_id=ced_inv["customer_id"], status="issued", created=ISSUED, voided_at="", currency="usd",
                   amount=309680, reason="order_change"))
    cnl.append(dict(id="cnli_demoCEDAR", credit_note_id="cn_demoCEDAR", invoice_line_item_id=ced_line["id"],
                    description="October allowance per AM-OF-2026-157 (37,742 runs)", amount=309680, quantity="",
                    service_start="2026-10-01", service_end_exclusive="2026-11-01", service_period_source="original invoice line"))
    write("stripe_credit_notes.csv", cn, cn_cols)
    write("stripe_credit_note_lines.csv", cnl, cnl_cols)

    gl, gl_cols = rows("netsuite_gl_detail.csv")

    def je(iid, ttype, doc, date, period, legs, ext="", plan="", created=CREATED_JE):
        for n, (acct, ns, dr, cr, memo) in enumerate(legs, 1):
            gl.append({"Internal ID": iid, "Line ID": n, "Transaction Type": ttype, "Document Number": doc, "Date": date,
                       "Posting Period": period, "Account Number": acct, "Customer Internal ID": ns, "Debit": f"{dr:.2f}",
                       "Credit": f"{cr:.2f}", "Memo": memo, "External Source ID": ext, "Revenue Plan ID": plan,
                       "Created At": created})

    J, C, Y, M = (cus[k][1] for k in ("Juniper Signal Works", "Cedar Harbor Labs", "Yarrow Ridge", "Meridian Process Systems"))
    # approved October journal entries (October period)
    je("JE-demo-F01", "Journal", f"ADJ-OCT-{J}", "2026-10-31", "Oct 2026", [("1150", J, 3135.22, 0, "October usage accrual correction"), ("4010", J, 0, 3135.22, "October usage accrual correction")])
    je("JE-demo-F02", "Journal", f"ADJ-OCT-{C}", "2026-10-31", "Oct 2026", [("4010", C, 3096.80, 0, "October usage accrual correction"), ("1150", C, 0, 3096.80, "October usage accrual correction")])
    je("JE-demo-F03", "Journal", f"ADJ-OCT-{Y}", "2026-10-31", "Oct 2026", [("1150", Y, 3074.28, 0, "October usage accrual"), ("4010", Y, 0, 3074.28, "October usage accrual")])
    je("JE-demo-F12", "Journal", f"REV-OCT-{M}-INC", "2026-10-31", "Oct 2026", [("2200", M, 526.03, 0, "October upgrade increment recognition"), ("4000", M, 0, 526.03, "October upgrade increment recognition")], plan="RP-demo-335780-INC")
    je("JE-demo-F12CA", "Journal", f"RECLASS-OCT-{M}-INC", "2026-10-31", "Oct 2026", [("1160", M, 526.03, 0, "Incremental platform contract asset"), ("2200", M, 0, 526.03, "Incremental platform contract asset")])
    # November billing documents
    for (iid, num, ns), (acct, amt) in zip(new_docs, [("1150", 3135.22), ("1150", 3074.28), ("1150", 100.00), ("2200", 526.03)]):
        je(f"JE-{iid}", "Invoice", num, "2026-11-13", "Nov 2026",
           [("1100", ns, amt, 0, "Invoice receivable"), (acct, ns, 0, amt, "Release October usage accrual" if acct == "1150" else "Platform billing")],
           ext=iid, created=ISSUED)
    je("JE-demo-CN-CEDAR", "Credit Memo", f"CN-{C}-20261113", "2026-11-13", "Nov 2026",
       [("1150", C, 3096.80, 0, "October allowance correction"), ("1100", C, 0, 3096.80, "Reduce invoice receivable")], ext="cn_demoCEDAR", created=ISSUED)
    write("netsuite_gl_detail.csv", gl, gl_cols)

    plan, plan_cols = rows("netsuite_revenue_plan_detail.csv")
    months = [("2026-10-16", "2026-11-01", "Oct 2026", "526.03", "526.03", "JE-demo-F12")]
    import datetime as dt
    start = dt.date(2026, 11, 1)
    total, acc_amt = 10520.55, 526.03
    while start < dt.date(2027, 9, 1):
        nxt = dt.date(start.year + (start.month // 12), start.month % 12 + 1, 1)
        amt = round(12000 * (nxt - start).days / 365 + 1e-9, 2)
        if nxt == dt.date(2027, 9, 1):
            amt = round(total - acc_amt, 2)
        acc_amt += amt
        months.append((start.isoformat(), nxt.isoformat(), start.strftime("%b %Y"), f"{amt:.2f}", "0.00", ""))
        start = nxt
    for s, e, p, pa, ra, j in months:
        plan.append({"Revenue Plan ID": "RP-demo-335780-INC", "Revenue Element ID": f"RE-{M}-INC", "Customer Internal ID": M,
                     "Source Document ID": "AM-OF-2026-164", "Plan Type": "Actual", "Recognition Start": s,
                     "Recognition End Exclusive": e, "Posting Period": p, "Planned Amount": pa, "Recognized Amount": ra,
                     "Journal Internal ID": j, "Plan Updated At": CREATED_JE})
    write("netsuite_revenue_plan_detail.csv", plan, plan_cols)

    counts = {"usage_batch_controls.csv": 61, "stripe_invoices.csv": len(inv), "stripe_invoice_lines.csv": len(lines),
              "stripe_credit_notes.csv": len(cn), "stripe_credit_note_lines.csv": len(cnl),
              "netsuite_opening_balances.csv": 120, "netsuite_gl_detail.csv": len(gl),
              "netsuite_revenue_plan_detail.csv": len(plan), "salesforce_accounts.csv": 30,
              "product_account_mapping.csv": 32, "usage_unique_records": 1924}
    with open(os.path.join(DST, "expected_counts.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "expected_rows"])
        w.writerows(counts.items())
    with open(os.path.join(DST, "README.txt"), "w") as fh:
        fh.write(f"October 2026 packet AFTER the approved corrections were booked (cutoff {CUTOFF}).\n"
                 "Adds: 5 approved October JEs, 4 November invoices (Juniper, Yarrow, Beacon, Meridian catch-up),\n"
                 "1 Cedar credit note and the Meridian incremental revenue plan element.\n"
                 "Rerun with cutoff 2026-11-14 18:00: every October error should clear; open items remain open.\n")
    print("written", DST)


if __name__ == "__main__":
    main()
