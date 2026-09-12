"""Recurrence detection and forecast-projection tests (synthetic data)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from buyorwait.config import EngineConfig
from buyorwait.forecast import build_items, series_occurrences
from conftest import run

R = date(2026, 3, 5)


def series(ledger, key):
    return [s for s in ledger.series if s.key == key]


def test_two_income_sub_series_in_one_category(mini):
    mini.profile(balance="5000", minimum="1000", methods="full_payment", max_months="")
    mini.monthly(15, "2000", 5, R, category="salary", event_type="income", description="Primary household salary", direction="credit")
    mini.monthly(20, "900", 5, R, category="salary", event_type="income", description="Second household income", direction="credit")
    mini.request(amount="100", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "100", 1, "2026-03-05", "", "0", "100")
    dec, ledger, an = run(mini)
    inc = series(ledger, "income|salary|credit")
    assert sorted(s.day_of_month for s in inc) == [15, 20]
    assert all(s.active for s in inc)


def test_missed_month_makes_income_inactive(mini):
    mini.profile(balance="5000", minimum="1000", methods="full_payment", max_months="")
    mini.monthly(15, "2000", 5, R, category="salary", event_type="income", description="Payroll credit", direction="credit")
    # second income stopped two months before the request (last on 20 Dec, none in Jan/Feb)
    mini.monthly(20, "900", 5, R - timedelta(days=60), category="salary", event_type="income", description="Second household income", direction="credit")
    mini.request(amount="100", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "100", 1, "2026-03-05", "", "0", "100")
    dec, ledger, an = run(mini)
    inc = {s.day_of_month: s.active for s in series(ledger, "income|salary|credit")}
    assert inc == {15: True, 20: False}


def test_variable_income_not_projected_but_fixed_mode_is(mini):
    mini.profile(balance="5000", minimum="1000", methods="full_payment", max_months="")
    amounts = ["1200", "1350", "1180", "1420", "1290"]
    for k, a in enumerate(amounts):
        mini.event(event_type="income", description="Weekly app earnings", category="salary", direction="credit", amount=a,
                   event_date=(R - timedelta(days=7 * (5 - k))).isoformat())
    mini.request(amount="100", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "100", 1, "2026-03-05", "", "0", "100")
    dec, ledger, an = run(mini)
    inc = series(ledger, "income|salary|credit")
    assert inc and not inc[0].active and any("variable-amount income" in n for n in inc[0].notes)
    # fixed salary with one reduced month: mode amount, still active
    mini.events = []
    for k, a in enumerate(["2000", "2000", "2000", "1100", "2000"]):
        mini.event(event_type="income", description="Payroll credit", category="salary", direction="credit", amount=a,
                   event_date=(date(2025, 10, 15) + timedelta(days=30 * k)).replace(day=15).isoformat())
    dec, ledger, an = run(mini)
    inc = series(ledger, "income|salary|credit")
    assert inc[0].active and inc[0].amount == Decimal("2000")


def test_final_payroll_description_ends_income(mini):
    mini.profile(balance="5000", minimum="1000", methods="full_payment", max_months="")
    ids = mini.monthly(15, "2000", 5, R, category="salary", event_type="income", description="Payroll credit", direction="credit")
    for e in mini.events:
        if e["event_id"] == ids[-1]:
            e["description"] = "Final employer payroll"
    mini.request(amount="100", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "100", 1, "2026-03-05", "", "0", "100")
    dec, ledger, an = run(mini)
    assert not series(ledger, "income|salary|credit")[0].active


def test_scheduled_next_salary_defines_amount_and_replaces_projection(mini):
    mini.profile(balance="5000", minimum="1000", methods="full_payment", max_months="")
    mini.event(event_type="income", description="Prorated first salary", category="salary", direction="credit", amount="900",
               event_date="2026-02-15")
    mini.event(event_type="income", description="Next confirmed salary", category="salary", direction="credit", amount="2300",
               event_date="2026-03-15", status="scheduled")
    mini.request(amount="100", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "100", 1, "2026-03-05", "", "0", "100")
    dec, ledger, an = run(mini)
    items = build_items(ledger, EngineConfig())
    credits = [(c.day, c.amount, c.kind) for c in items if c.amount > 0]
    assert credits[0] == (date(2026, 3, 15), Decimal("2300.00"), "scheduled")  # explicit row, not a duplicate projection
    assert sum(1 for d, _, _ in credits if d == date(2026, 3, 15)) == 1
    assert (date(2026, 4, 15), Decimal("2300.00"), "recurring") in credits


def test_extra_one_off_inside_weekly_cadence_is_dropped(mini):
    mini.profile(balance="5000", minimum="1000", methods="full_payment", max_months="")
    mini.monthly(15, "2000", 5, R, category="salary", event_type="income", description="Payroll credit", direction="credit")
    mini.interval(7, ["50", "60", "55"], R, category="groceries")
    mini.event(description="Bulk groceries and pantry purchase", category="groceries", amount="900", event_date=(R - timedelta(days=9)).isoformat())
    mini.request(amount="100", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "100", 1, "2026-03-05", "", "0", "100")
    dec, ledger, an = run(mini)
    g = series(ledger, "expense|groceries|debit")[0]
    assert g.cadence == "interval" and g.interval_days == 7
    assert g.amount == Decimal("55.00")  # midrange of 50..60, the 900 one-off is not part of the series


def test_interval_phase_replays_history_shifted_180_days(mini):
    mini.profile(balance="5000", minimum="1000", methods="full_payment", max_months="")
    mini.monthly(15, "2000", 5, R, category="salary", event_type="income", description="Payroll credit", direction="credit")
    mini.interval(10, ["40"], R, category="groceries", offset=4)  # first occurrence = R-180+4
    mini.request(amount="100", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "100", 1, "2026-03-05", "", "0", "100")
    dec, ledger, an = run(mini)
    g = series(ledger, "expense|groceries|debit")[0]
    occ = series_occurrences(g, ledger.start, ledger.end, EngineConfig())
    assert occ[0] == R + timedelta(days=4)
    assert all((occ[k + 1] - occ[k]).days == 10 for k in range(len(occ) - 1))
    assert occ[-1] <= R + timedelta(days=90)


def test_monthly_templates_stop_after_second_following_month(mini):
    mini.profile(balance="5000", minimum="1000", methods="full_payment", max_months="")
    mini.monthly(15, "2000", 5, R, category="salary", event_type="income", description="Payroll credit", direction="credit")
    mini.monthly(2, "800", 5, R, category="rent")
    mini.interval(7, ["10"], R, category="groceries", offset=2)
    mini.request(amount="100", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "100", 1, "2026-03-05", "", "0", "100")
    dec, ledger, an = run(mini)
    items = build_items(ledger, EngineConfig())
    rent_days = [c.day for c in items if c.label.startswith("rent")]
    assert rent_days == [date(2026, 4, 2), date(2026, 5, 2)]  # 2 June is inside 90 days but outside month M+2
    assert ledger.end == R + timedelta(days=90)
    last_grocery = max(c.day for c in items if c.label.startswith("groceries"))
    assert last_grocery >= R + timedelta(days=90 - 6)  # interval items run to R+90 (last weekly slot)


def test_payday_expense_and_income_net_out_end_of_day(mini):
    mini.profile(balance="1000", minimum="900", methods="full_payment", max_months="")
    mini.monthly(15, "2000", 5, R, category="salary", event_type="income", description="Payroll credit", direction="credit")
    mini.monthly(15, "300", 5, R, category="family_support", description="Childcare contribution")
    mini.request(amount="100", deadline="2026-04-30", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "100", 1, "2026-03-05", "", "0", "100")
    dec, ledger, an = run(mini)
    # paying 100 today leaves 900 = minimum until payday; on payday the 300 debit is netted against 2000 income
    assert dec.affordability_status == "affordable_now"


def test_no_history_user_has_no_series_and_conservative_answer(mini):
    mini.profile(balance="1500", minimum="1000", methods="full_payment|installments", max_months="3")
    mini.request(amount="800", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "800", 1, "2026-03-05", "", "0", "800")
    mini.option("payment_option_2", "request_1", "installments", "280", 3, "2026-03-08", "30", "40", "840")
    dec, ledger, an = run(mini)
    assert ledger.series == [] and dec.amount_safe_to_pay == Decimal("500.00")
    assert dec.affordability_status == "not_affordable"  # no income is ever invented


def test_deadline_beyond_horizon_and_missing_rate_are_handled(mini):
    mini.profile(currency="INR", balance="150000", minimum="50000", methods="full_payment", max_months="")
    mini.monthly(15, "1000", 5, R, category="salary", event_type="income", description="International employer payroll",
                 direction="credit", currency="USD")  # no USD->INR rate supplied at all
    mini.request(amount="120000", deadline="2026-12-31", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "120000", 1, "2026-03-05", "", "0", "120000")
    dec, ledger, an = run(mini)
    assert any("no supplied exchange rate" in n for n in ledger.notes)
    assert dec.amount_safe_to_pay == Decimal("100000.00")  # income never guessed; only the balance headroom counts
    assert dec.affordability_status == "not_affordable"
