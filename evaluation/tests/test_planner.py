"""Candidate ranking and spending-change tests (synthetic data)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from buyorwait.models import Plan, SpendingChange
from buyorwait.planner import enumerate_changes, rank_key
from conftest import run

R = date(2026, 3, 5)


def user(mini, balance, minimum="1000", methods="full_payment|partial_payment|installments", max_months="6", salary="2000",
         reduce="dining", stop="streaming", protect="rent"):
    mini.profile(balance=balance, minimum=minimum, methods=methods, max_months=max_months, reduce=reduce, stop=stop, protect=protect)
    mini.monthly(15, salary, 5, R, category="salary", event_type="income", description="Payroll credit", direction="credit")
    mini.monthly(2, "800", 5, R, category="rent")


def full_opt(mini, amount):
    mini.option("payment_option_1", "request_1", "full_payment", amount, 1, R.isoformat(), "", "0", amount)


def test_rank_key_orders_by_contest_tie_breakers():
    d = date(2026, 3, 5)
    on_time_free = Plan("wait", [(d, Decimal(100))], [], completes_by_deadline=True, total_paid=Decimal(100))
    on_time_changes = Plan("full_payment", [(d, Decimal(100))], [SpendingChange("stop", "event_1", "k")], completes_by_deadline=True, total_paid=Decimal(100))
    late_free = Plan("wait", [(d, Decimal(100))], [], completes_by_deadline=False, total_paid=Decimal(100))
    dearer = Plan("installments", [(d, Decimal(55)), (d, Decimal(55))], [], completes_by_deadline=True, total_paid=Decimal(110), option_number=2)
    later_start = Plan("wait", [(date(2026, 3, 20), Decimal(100))], [], completes_by_deadline=True, total_paid=Decimal(100))
    more_payments = Plan("partial_payment", [(d, Decimal(40)), (date(2026, 3, 15), Decimal(60))], [], completes_by_deadline=True, total_paid=Decimal(100))
    ordered = sorted([late_free, dearer, on_time_changes, later_start, more_payments, on_time_free], key=rank_key)
    assert ordered[0] is on_time_free
    assert ordered[1] is more_payments  # same total & start as on_time_free but two payments
    assert ordered[2] is later_start
    assert ordered[3] is dearer
    assert ordered[4] is on_time_changes
    assert ordered[-1] is late_free


def test_partial_beats_wait_when_both_complete_by_deadline(mini):
    user(mini, balance="1400")
    mini.request(amount="1000", deadline="2026-04-10", allows_partial="true")
    full_opt(mini, "1000")
    dec, ledger, an = run(mini)
    methods = [p.method for p in an.candidates]
    assert "wait" in methods and "partial_payment" in methods
    assert dec.recommended_payment_method == "partial_payment"  # same total, earlier start


def test_wait_beats_instalments_when_user_accepts_full_payment(mini):
    user(mini, balance="1400", max_months="3")
    mini.request(amount="1000", deadline="2026-05-30", allows_partial="false")
    full_opt(mini, "1000")
    mini.option("payment_option_2", "request_1", "installments", "350", 3, "2026-03-08", "30", "50", "1050")
    dec, ledger, an = run(mini)
    assert {p.method for p in an.candidates} >= {"wait", "installments"}
    assert dec.recommended_payment_method == "wait" and dec.payment_plan == "2026-03-15:1000"


def test_full_payment_today_beats_everything_when_safe(mini):
    user(mini, balance="5000", max_months="3")
    mini.request(amount="1000", deadline="2026-05-30", allows_partial="true")
    full_opt(mini, "1000")
    mini.option("payment_option_2", "request_1", "installments", "350", 3, "2026-03-08", "30", "50", "1050")
    dec, ledger, an = run(mini)
    assert dec.affordability_status == "affordable_now" and dec.recommended_payment_method == "full_payment"
    assert dec.earliest_date_for_full_payment == R and dec.payment_plan == "2026-03-05:1000"


def test_late_wait_is_reported_as_affordable_later_when_nothing_else_works(mini):
    user(mini, balance="1400", methods="full_payment", max_months="", reduce="", stop="")
    mini.request(amount="1000", deadline="2026-03-10", allows_partial="false")
    full_opt(mini, "1000")
    dec, ledger, an = run(mini)
    assert dec.recommended_payment_method == "wait" and dec.affordability_status == "affordable_later"
    assert dec.payment_plan == "2026-03-15:1000" and not an.chosen.completes_by_deadline


def test_lowest_option_id_breaks_exact_ties(mini):
    user(mini, balance="1400", methods="installments", max_months="6")
    mini.request(amount="1000", deadline="2026-05-30", allows_partial="false")
    full_opt(mini, "1000")
    mini.option("payment_option_3", "request_1", "installments", "350", 3, "2026-03-08", "30", "50", "1050")
    mini.option("payment_option_2", "request_1", "installments", "350", 3, "2026-03-08", "30", "50", "1050")
    dec, ledger, an = run(mini)
    assert an.chosen.option_id == "payment_option_2"


def test_change_enumeration_respects_limits_and_exclusivity(mini):
    user(mini, balance="1300", methods="full_payment", max_months="", reduce="dining|shopping|entertainment|gym", stop="streaming|cloud_storage|gym")
    mini.monthly(10, "40", 5, R, category="streaming", event_type="subscription", description="Streaming", flexibility="stoppable")
    mini.monthly(11, "10", 5, R, category="cloud_storage", event_type="subscription", description="Cloud", flexibility="stoppable")
    mini.monthly(12, "100", 5, R, category="dining", description="Takeaway", flexibility="reducible", min_allowed="20")
    mini.monthly(13, "90", 5, R, category="shopping", description="Shopping", flexibility="reducible", min_allowed="30")
    mini.monthly(14, "60", 5, R, category="gym", event_type="subscription", description="Gym", flexibility="reducible_or_stoppable", min_allowed="25")
    mini.monthly(9, "70", 5, R, category="entertainment", description="Cinema", flexibility="reducible", min_allowed="35")
    mini.request(amount="100", allows_partial="false")
    full_opt(mini, "100")
    dec, ledger, an = run(mini)
    combos = enumerate_changes(ledger, __import__("buyorwait.config", fromlist=["EngineConfig"]).EngineConfig())
    assert combos and all(len(c) <= 3 for c in combos)
    for combo in combos:
        keys = [c.series_key for c in combo]
        assert len(set(keys)) == len(keys)  # never stop and reduce the same series
    gym_actions = {c.action for combo in combos for c in combo if c.series_key.endswith("gym|debit")}
    assert gym_actions == {"stop", "reduce_to"}  # both forms offered, never together
    reds = [c for combo in combos for c in combo if c.action == "reduce_to"]
    assert all(c.new_amount is not None for c in reds)
    # combos are ordered by total reduction (smallest first): cloud stop (10/month) is the very first
    assert combos[0][0].action == "stop" and combos[0][0].series_key.endswith("cloud_storage|debit")


def test_change_references_latest_occurrence_and_minimum_allowed_amount(mini):
    user(mini, balance="1200", methods="full_payment", max_months="", reduce="dining", stop="")
    ids = mini.monthly(12, "100", 5, R, category="dining", description="Takeaway", flexibility="reducible", min_allowed="20")
    mini.request(amount="260", deadline="2026-03-13", allows_partial="false")  # 1200-1000-100 = 100 safe; reduce saves 80 -> 180 < 260
    full_opt(mini, "260")
    dec, ledger, an = run(mini)
    assert dec.spending_changes_needed == "none"  # not enough even with the cut
    mini.requests[0]["requested_amount"] = "180"
    mini.options[0]["payment_amount"] = mini.options[0]["total_payable_amount"] = "180"
    dec, ledger, an = run(mini)
    assert dec.spending_changes_needed == f"reduce_to:{ids[-1]}:20"
    assert dec.affordability_status == "affordable_with_plan" and dec.recommended_payment_method == "full_payment"
    assert dec.earliest_date_for_full_payment == date(2026, 3, 15)  # change-free capacity date is still reported


def test_protected_and_non_permitted_categories_are_never_changed(mini):
    user(mini, balance="1200", methods="full_payment", max_months="", reduce="dining", stop="streaming", protect="rent|dining")
    mini.monthly(12, "100", 5, R, category="dining", description="Takeaway", flexibility="reducible", min_allowed="20")
    mini.monthly(10, "40", 5, R, category="music_subscription", event_type="subscription", description="Music", flexibility="stoppable")
    mini.request(amount="150", deadline="2026-03-13", allows_partial="false")
    full_opt(mini, "150")
    dec, ledger, an = run(mini)
    assert ledger.flexible_series() == []  # dining protected, music not in the stop list
    assert dec.spending_changes_needed == "none"


def test_instalments_with_changes_when_user_rejects_full_payment(mini):
    user(mini, balance="1300", methods="installments", max_months="3", reduce="dining", stop="streaming")
    mini.monthly(10, "40", 5, R, category="streaming", event_type="subscription", description="Streaming", flexibility="stoppable")
    mini.request(amount="800", deadline="2026-05-30", allows_partial="false")
    full_opt(mini, "800")
    mini.option("payment_option_2", "request_1", "installments", "290", 3, "2026-03-12", "30", "70", "870")
    dec, ledger, an = run(mini)
    # 1300 - 40 (streaming, 10 Mar) - 290 (12 Mar) = 970 < 1000; stopping streaming leaves 1010 -> safe
    assert dec.recommended_payment_method == "installments"
    assert dec.spending_changes_needed.startswith("stop:")
    assert dec.affordability_status == "affordable_with_plan"
    assert dec.payment_plan == "2026-03-12:290|2026-04-11:290|2026-05-11:290"
