"""Synthetic unit/property tests for the deterministic engine.

Each test builds a tiny dataset in a temp directory (see conftest.MiniData), runs the pipeline and checks
the financial rule under test. No real dataset rows or sample answers are used.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from conftest import run

R = date(2026, 3, 5)


def base_user(mini, balance="3000", minimum="1000", methods="full_payment|partial_payment|installments", max_months="6",
              salary_months=5, salary="2000", rent="800"):
    mini.profile(balance=balance, minimum=minimum, methods=methods, max_months=max_months)
    mini.monthly(15, salary, salary_months, R, category="salary", event_type="income", description="Payroll credit", direction="credit")
    mini.monthly(2, rent, salary_months, R, category="rent", description="Rent")


def test_confirmed_future_salary_counted_on_settlement_date(mini):
    base_user(mini, balance="1500", minimum="1000", rent="800")
    mini.request(amount="1200", deadline="2026-04-30", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "1200", 1, "2026-03-05", "", "0", "1200")
    dec, ledger, an = run(mini)
    # 1500 - 800 rent (04-02 is after payday) ... before 03-15 nothing is due: safe = 500, earliest = payday
    assert dec.amount_safe_to_pay == Decimal("500.00")
    assert dec.earliest_date_for_full_payment == date(2026, 3, 15)
    assert dec.recommended_payment_method == "wait"
    assert dec.affordability_status == "affordable_later"


def test_pending_credit_ignored_and_pending_debit_reserved(mini):
    base_user(mini, balance="3000", minimum="1000")
    mini.event(event_type="refund", description="Pending merchant refund", category="shopping", direction="credit", amount="900",
               event_date="2026-03-04", settlement_date="2026-03-09", status="pending")
    mini.event(description="Pending merchant debit", category="shopping", amount="150", event_date="2026-03-04",
               settlement_date="2026-03-08", status="pending")
    mini.request(amount="5000", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "5000", 1, "2026-03-05", "", "0", "5000")
    dec, ledger, an = run(mini)
    kinds = {(c.kind, str(c.amount)) for c in ledger.one_offs}
    assert ("pending", "-150.00") in kinds
    assert not any(c.amount > 0 and c.kind == "pending" for c in ledger.one_offs)
    assert dec.amount_safe_to_pay == Decimal("1850.00")  # 3000 - 1000 - 150 (rent hits after payday)


def test_cancelled_and_failed_rows_ignored_and_message_amends_salary_date(mini):
    base_user(mini, balance="1500", minimum="1000")
    mini.event(description="Cancelled card authorization", category="shopping", amount="400", event_date="2026-03-03",
               settlement_date="2026-03-06", status="cancelled")
    mini.event(description="Failed utility debit", category="utilities", amount="300", event_date="2026-03-04", status="failed")
    mini.message("BrightPath Media has updated your payroll record. Your confirmed salary is now expected on 2026-03-23. "
                 "This replaces the payroll date shown in the earlier update. Payroll ref EMP-0001.")
    mini.request(amount="1200", deadline="2026-04-30", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "1200", 1, "2026-03-05", "", "0", "1200")
    dec, ledger, an = run(mini)
    assert not ledger.one_offs  # cancelled + failed rows never reserved
    assert dec.earliest_date_for_full_payment == date(2026, 3, 23)  # amended payroll date wins over history


def test_duplicate_charge_ignored_unless_dispute_message_keeps_it(mini):
    base_user(mini, balance="3000", minimum="1000")
    orig = mini.event(description="Original card charge", category="shopping", amount="200", event_date="2026-03-01")
    dup = mini.event(description="Possible duplicate card charge", category="shopping", amount="200", event_date="2026-03-03",
                     settlement_date="2026-03-07", status="pending", linked_event_id=orig)
    mini.request(amount="5000", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "5000", 1, "2026-03-05", "", "0", "5000")
    dec, ledger, an = run(mini)
    assert dec.amount_safe_to_pay == Decimal("2000.00")
    mini.message("Here's the latest transaction update from Summit Bank. The extra card charge is still being investigated. "
                 "A reversal has not been posted to the account yet. Txn ref BAN-0001.", related=dup, source="bank")
    dec, ledger, an = run(mini)
    assert dec.amount_safe_to_pay == Decimal("1800.00")


def test_only_flexible_permitted_categories_are_changed(mini):
    mini.profile(balance="1300", minimum="1000", methods="full_payment", max_months="", protect="rent|groceries", reduce="dining",
                 stop="streaming")
    mini.monthly(15, "2000", 5, R, category="salary", event_type="income", description="Payroll credit", direction="credit")
    mini.monthly(2, "800", 5, R, category="rent")
    stream = mini.monthly(10, "40", 5, R, category="streaming", event_type="subscription", description="Family streaming plan",
                          flexibility="stoppable")
    groc = mini.monthly(12, "100", 5, R, category="groceries", description="Groceries", flexibility="reducible", min_allowed="50")
    mini.request(amount="240", deadline="2026-03-12", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "240", 1, "2026-03-05", "", "0", "240")
    dec, ledger, an = run(mini)
    # safe today = 1300 - 1000 - 40 - 100 = 160 < 240; stopping streaming (+40) and cutting groceries would reach it,
    # but groceries are protected -> only the streaming change is legal and it is not enough -> wait after deadline
    assert dec.spending_changes_needed == "none"
    assert dec.recommended_payment_method == "wait"
    # now allow a big enough flexible cut
    mini.events = [e for e in mini.events if e["category"] != "groceries"]
    mini.monthly(12, "100", 5, R, category="dining", description="Takeaway", flexibility="reducible", min_allowed="20")
    dec, ledger, an = run(mini)
    assert dec.recommended_payment_method == "full_payment"
    assert dec.affordability_status == "affordable_with_plan"
    assert dec.spending_changes_needed.startswith("reduce_to:") and ":20" in dec.spending_changes_needed
    assert stream[-1] not in dec.spending_changes_needed  # smallest sufficient change only


def test_foreign_currency_converted_with_settlement_date_rate(mini):
    mini.profile(currency="INR", balance="100000", minimum="50000", methods="full_payment", max_months="")
    mini.monthly(15, "1000", 5, R, category="salary", event_type="income", description="International employer payroll",
                 direction="credit", currency="USD")
    mini.rate("2026-03-15", "USD", "INR", "80")
    mini.rate("2026-02-15", "USD", "INR", "70")
    mini.request(amount="120000", deadline="2026-04-30", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "120000", 1, "2026-03-05", "", "0", "120000")
    dec, ledger, an = run(mini)
    inc = [c for c in dec.trace["series"] if c["key"].startswith("income")][0]
    assert inc["currency"] == "USD"
    from buyorwait.forecast import build_items
    from buyorwait.config import EngineConfig

    items = build_items(ledger, EngineConfig())
    march = [c for c in items if c.day == date(2026, 3, 15) and c.amount > 0]
    assert march and march[0].amount == Decimal("80000.00")  # 1000 USD x 80 (rate dated 2026-03-15)


def test_image_only_amount_is_never_zero(mini, tmp_path):
    base_user(mini, balance="3000", minimum="1000")
    ev = mini.event(description="Outstanding rent balance", category="rent", amount="", event_date="2026-03-04",
                    settlement_date="2026-03-09", status="scheduled")
    mini.image("image_x", ev)
    mini.request(amount="5000", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "5000", 1, "2026-03-05", "", "0", "5000")
    root = mini.write()
    # write a tiny png and a cache entry keyed by its hash
    import hashlib
    png = b"\x89PNG\r\n\x1a\n" + b"synthetic"
    p = f"{root}/media/images/image_x.png"
    with open(p, "wb") as f:
        f.write(png)
    h = hashlib.sha256(png).hexdigest()
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({h: {"image_id": "image_x", "amount": "250", "currency": "EUR", "confidence": 0.9, "method": "test"}}))
    from buyorwait.config import EngineConfig
    from buyorwait.images import ImageExtractor
    from buyorwait.ingest import load_dataset
    from buyorwait.pipeline import Engine

    ds = load_dataset(root)
    engine = Engine(ds, EngineConfig(), ImageExtractor(cache_path=str(cache)))
    dec, ledger, an = engine.decide(ds.requests[0])
    assert any(c.amount == Decimal("-250.00") for c in ledger.one_offs)
    # without a cache the row is excluded (not treated as zero) and a note is written
    engine2 = Engine(ds, EngineConfig(), ImageExtractor(cache_path=str(tmp_path / "missing.json"), enable_vlm=False))
    dec2, ledger2, an2 = engine2.decide(ds.requests[0])
    assert not ledger2.one_offs and any("never zero" in n for n in ledger2.notes)


def test_installments_follow_supplied_option_and_respect_max_months(mini):
    base_user(mini, balance="1500", minimum="1000", methods="installments", max_months="3")
    mini.request(amount="900", deadline="2026-05-30", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "900", 1, "2026-03-05", "", "0", "900")
    mini.option("payment_option_2", "request_1", "installments", "310", 3, "2026-03-08", "30", "30", "930")
    mini.option("payment_option_3", "request_1", "installments", "160", 6, "2026-03-08", "30", "60", "960")
    dec, ledger, an = run(mini)
    assert dec.recommended_payment_method == "installments"
    assert dec.payment_plan == "2026-03-08:310|2026-04-07:310|2026-05-07:310"
    assert any("payment_option_3" in r and "max_installment_months" in r for r in an.rejected)
    # earliest date is capacity-based, independent of the user's refusal of full payment
    assert dec.earliest_date_for_full_payment == date(2026, 3, 15)


def test_partial_payment_exactly_two_payments_summing_to_request(mini):
    base_user(mini, balance="1400", minimum="1000", methods="partial_payment|installments", max_months="2")
    mini.request(amount="1000", deadline="2026-04-10", allows_partial="true")
    mini.option("payment_option_1", "request_1", "full_payment", "1000", 1, "2026-03-05", "", "0", "1000")
    mini.option("payment_option_2", "request_1", "installments", "520", 2, "2026-03-05", "28", "40", "1040")
    dec, ledger, an = run(mini)
    assert dec.recommended_payment_method == "partial_payment"
    assert dec.amount_safe_to_pay == Decimal("400.00")
    assert dec.payment_plan == "2026-03-05:400|2026-03-15:600"  # cheaper than the 1040 instalment option
    assert dec.affordability_status == "affordable_with_plan"


def test_minimum_balance_boundary_is_strict(mini):
    base_user(mini, balance="1300", minimum="1000", methods="full_payment", max_months="")
    mini.request(amount="300", deadline="2026-03-10", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "300", 1, "2026-03-05", "", "0", "300")
    dec, ledger, an = run(mini)
    assert dec.amount_safe_to_pay == Decimal("300.00") and dec.affordability_status == "affordable_now"
    mini.requests[0]["requested_amount"] = "300.01"
    mini.options[0]["payment_amount"] = "300.01"
    mini.options[0]["total_payable_amount"] = "300.01"
    dec, ledger, an = run(mini)
    assert dec.amount_safe_to_pay == Decimal("300.00")
    assert dec.affordability_status != "affordable_now"


def test_all_plans_unavailable_gives_not_affordable(mini):
    base_user(mini, balance="1200", minimum="1000", methods="installments", max_months="2", salary="500", rent="800")
    mini.request(amount="5000", deadline="2026-04-10", allows_partial="true")
    mini.option("payment_option_1", "request_1", "full_payment", "5000", 1, "2026-03-05", "", "0", "5000")
    mini.option("payment_option_2", "request_1", "installments", "900", 6, "2026-03-08", "30", "400", "5400")
    dec, ledger, an = run(mini)
    assert dec.affordability_status == "not_affordable"
    assert dec.recommended_payment_method == "not_recommended"
    assert dec.payment_plan == "none" and dec.spending_changes_needed == "none"
    assert dec.earliest_date_for_full_payment is None
    assert Decimal(0) <= dec.amount_safe_to_pay <= Decimal("5000")


def test_output_formatting_rules():
    from buyorwait.render import fmt_amount, fmt_plan_amount, render_plan

    assert fmt_amount(Decimal("603.30")) == "603.3"
    assert fmt_amount(Decimal("737.00")) == "737"
    assert fmt_amount(Decimal("17229139.20")) == "17229139.2"
    assert fmt_plan_amount(Decimal("620.4")) == "620.40"
    assert fmt_plan_amount(Decimal("25256")) == "25256"
    assert fmt_plan_amount(Decimal("23.5")) == "23.50"
    assert render_plan([(date(2026, 9, 7), Decimal("300")), (date(2026, 10, 7), Decimal("300"))]) == "2026-09-07:300|2026-10-07:300"
    assert render_plan([]) == "none"
