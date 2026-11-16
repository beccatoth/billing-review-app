"""Golden tests: the bundled October 2026 sample must reproduce the reviewed results to the cent."""
from decimal import Decimal as D

import pytest

from engine.packet import DEMO_CUTOFF, DEMO_DIR, SAMPLE_DIR, load_baseline, prepare_month, sample_cutoff
from engine.review import ERROR, NEEDS_INFO, VALID, FOLLOW_UP, run_review
from engine.util import parse_ts


def run_month(folder, year=2026, month=10, cutoff=None):
    m = prepare_month(folder, load_baseline())
    return run_review(m["tables"], m["terms"], m["approvals"], year, month, cutoff or sample_cutoff(), m["expected_counts"]), m


@pytest.fixture(scope="module")
def r():
    return run_month(SAMPLE_DIR)[0]


def test_october_packet_adds_only_the_two_amendments():
    m = prepare_month(SAMPLE_DIR, load_baseline())
    assert sorted(m["new_docs"]) == ["AM-OF-2026-157", "AM-OF-2026-164"]
    assert len(load_baseline()["terms"]) == 30


def find(r, customer, cls=None):
    out = [f for f in r.findings if f.customer.startswith(customer) and (cls is None or f.classification == cls)]
    assert out, f"no finding for {customer}"
    return out[0]


def test_no_blocking_data_issues(r):
    assert r.blocking == []
    assert all(c["result"] != "Fail" for c in r.checks)


def test_usage_dedup_and_controls(r):
    assert len(r.usage_prep["raw"]) == 1925
    assert len(r.usage_prep["clean"]) == 1924
    assert list(r.usage_prep["exact_dupes"]["record_id"]) == ["ur_7352010ec2e04c"]


@pytest.mark.parametrize("customer,billing,revenue", [
    ("Juniper Signal", "3135.22", "3135.22"),
    ("Cedar Harbor", "-3096.80", "-3096.80"),
    ("Yarrow Ridge", "3074.28", "3074.28"),
    ("Beacon Marsh", "100.00", "0"),
    ("Meridian", "526.03", "526.03"),
])
def test_errors(r, customer, billing, revenue):
    f = find(r, customer, ERROR)
    assert f.billing_effect == D(billing)
    assert f.revenue_effect == D(revenue)


def test_cedar_day_weighted_allowance(r):
    cedar = [c for c in r.customers if c.name.startswith("Cedar")][0]
    u = r.usage[cedar.ns_id]
    assert u["allowance"] == 37742 and u["units"] == 58470 and u["net"] == D("8291.20")


def test_net_october_revenue_effect(r):
    total = sum(f.revenue_effect for f in r.findings if f.classification == ERROR)
    assert total == D("3638.73")


def test_valid_explanations(r):
    assert find(r, "Zephyr", VALID)
    assert find(r, "Aspen Vale", VALID)
    assert find(r, "Westhaven", VALID)
    assert find(r, "Alder", VALID)
    west = [c for c in r.customers if c.name.startswith("Westhaven")][0]
    assert r.usage[west.ns_id]["net"] == D("1593.30")
    zep = [c for c in r.customers if c.name.startswith("Zephyr")][0]
    assert r.usage[zep.ns_id]["gross"] == D("952.60") and r.usage[zep.ns_id]["net"] == D("852.60")


def test_needs_information_not_in_totals(r):
    f = find(r, "Unassigned workspace", NEEDS_INFO)
    assert f.billing_effect == 0 and f.revenue_effect == 0
    assert r.unverified[0]["units"] == 49762
    w = find(r, "Willow", NEEDS_INFO)
    assert w.revenue_effect == 0


def test_follow_up_meridian_november(r):
    f = find(r, "Meridian", FOLLOW_UP)
    assert "$1,000.00" in f.conditional_exposure


def test_journals_balance_and_amounts(r):
    assert all(j["balanced"] for j in r.journals)
    by = {j["je_id"]: j for j in r.journals}
    ca = [j for j in r.journals if j["je_id"].endswith("-CA")]
    assert len(ca) == 1 and ca[0]["lines"][0]["account"] == "1160" and ca[0]["lines"][0]["debit"] == D("526.03")
    assert len(r.journals) == 5


def test_rollforward_ties(r):
    rf = [x for x in r.rollforward if x["account"] == "1150"]
    assert len(rf) == 30 and all(x["ties"] for x in rf)
    meridian_1160 = [x for x in r.rollforward if x["account"] == "1160" and x["customer"].startswith("Meridian")][0]
    assert meridian_1160["corrected_close"] == D("547.95")


def test_crescent_first_month_proration(r):
    sep = run_month(SAMPLE_DIR, 2026, 9)[0]
    cres = [c for c in sep.customers if c.name.startswith("Crescent")][0]
    assert sep.usage[cres.ns_id]["allowance"] == 21333


def test_rerun_after_corrections_clears_every_error():
    """Rerun on the packet re-exported after the approved corrections were booked."""
    d, _ = run_month(DEMO_DIR, cutoff=parse_ts(DEMO_CUTOFF))
    assert all(c["result"] != "Fail" for c in d.checks)
    assert [f for f in d.findings if f.classification == ERROR] == []
    assert d.journals == [] and d.billing_docs == []
    # open items stay open – nothing is resolved by assumption
    assert len([f for f in d.findings if f.classification == NEEDS_INFO]) == 3
    assert all(x["ties"] for x in d.rollforward if x["account"] == "1150")


def test_messages_pdf_approvals(r):
    assert find(r, "Zephyr", VALID)
    assert not [f for f in r.findings if f.customer.startswith("Zephyr") and f.classification == NEEDS_INFO]
    assert find(r, "Willow", NEEDS_INFO)


def test_amendments_judged_from_stripe_not_register():
    r, _ = run_month(SAMPLE_DIR)
    assert r.amendment_status["AM-OF-2026-157"][0] == "Not reflected"   # invoice used 30,000 allowance
    assert r.amendment_status["AM-OF-2026-164"][0] == "Not reflected"   # catch-up not billed, Nov invoice $4,000
    assert not [c for c in r.checks if c["area"] == "Contracts" and c["result"] != "Pass"]


def test_baseline_round_trip_through_excel():
    from engine.packet import baseline_from_excel, baseline_to_excel
    m = prepare_month(SAMPLE_DIR, load_baseline())
    b = baseline_from_excel(baseline_to_excel(m["tables"], m["terms"], "Oct 2026 close"))
    assert b["label"] == "Oct 2026 close" and len(b["terms"]) == 32 and "register" not in b["tables"]


def test_september_snapshot_is_the_prior_close():
    b = load_baseline()
    c = b["snapshot"]["customers"]
    assert len(c) == 30 and set(c["period"]) == {"Sep 2026"}
    assert abs(c["usage_billed"].sum() - c["bal_1150"].sum()) < 0.01           # Sept usage billed Oct 1 = Sept 30 unbilled
    assert abs(c["bal_1150"].sum() - 82170.97) < 0.01                           # START_HERE / review: Sept 1150 total
    assert abs(c["bal_2200"].sum() + 225863.04) < 0.01                          # deferred revenue (credit) at Sept 30
    cres = c[c["customer"].str.startswith("Crescent")].iloc[0]
    assert cres["allowance"] == 21333                                           # first partial month


def test_october_snapshot_rolls_into_next_baseline():
    from engine.packet import baseline_from_excel, baseline_to_excel
    from engine.snapshot import build_snapshot
    r, m = run_month(SAMPLE_DIR)
    b = baseline_from_excel(baseline_to_excel(m["tables"], m["terms"], "Oct 2026 close", build_snapshot(r)))
    oc = b["snapshot"]["customers"]
    assert set(oc["period"]) == {"Oct 2026"} and len(oc) == 30
    jun = oc[oc["customer"].str.startswith("Juniper Signal")].iloc[0]
    assert abs(jun["usage_accrued"] - 4560.32) < 0.005                           # carried forward after correction


def test_billing_load_dates_live_in_the_repository():
    from engine.packet import apply_billing_load_dates
    b = load_baseline()
    assert (b["terms"]["billing_loaded_at"].str.startswith("2026-08-26")).all()      # set at Sept load
    m = prepare_month(SAMPLE_DIR, b)
    am = m["terms"][m["terms"]["document_type"] == "amendment"]
    assert (am["billing_loaded_at"] == "").all()                                        # never loaded to billing
    import pandas as pd
    t2, n = apply_billing_load_dates(m["terms"], pd.DataFrame([{"document_id": "OF-2026-118", "billing_loaded_at": "2027-01-01"}]))
    assert n == 0 and t2.loc[t2["document_id"] == "OF-2026-118", "billing_loaded_at"].iloc[0].startswith("2026-08-26")  # set once
