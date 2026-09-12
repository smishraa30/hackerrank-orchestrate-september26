"""Regression and safety tests added by the engineering audit.

Covers: as-of handling of messages, unknown/unconvertible future debits (conservative blocking), pending
balance semantics, later-only FX rates, monthly occurrences within 90 days, month-end/leap-year clamping,
partial-plan optimality, scheduled-vs-projected duplication, malformed CSV rows, permutation invariance,
image-evidence validation, and monotone safety properties.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import random
from datetime import date, timedelta
from decimal import Decimal

import pytest
from buyorwait.config import EngineConfig
from buyorwait.forecast import build_items
from buyorwait.images import ImageExtractor
from buyorwait.ingest import load_dataset
from buyorwait.pipeline import Engine
from buyorwait.render import decision_row
from conftest import MiniData, run

R = date(2026, 3, 5)


def base(mini, balance="3000", minimum="1000", methods="full_payment|partial_payment", **kw):
    mini.profile(balance=balance, minimum=minimum, methods=methods, max_months="", **kw)
    mini.monthly(15, "2000", 5, R, category="salary", event_type="income", description="Payroll credit", direction="credit")
    mini.monthly(2, "800", 5, R, category="rent")


def req(mini, amount="500", deadline="2026-04-30", partial="false"):
    mini.request(amount=amount, deadline=deadline, allows_partial=partial)
    mini.option("payment_option_1", "request_1", "full_payment", amount, 1, R.isoformat(), "", "0", amount)


# ---------------------------------------------------------------- as-of / leakage

def test_message_sent_after_request_date_is_ignored(mini):
    base(mini, balance="1500")
    req(mini, amount="1200")
    mini.message("Your confirmed salary is now expected on 2026-03-10. This replaces the payroll date shown in the earlier update.",
                 sent="2026-03-07T09:30:00Z")  # two days AFTER the request date
    dec, ledger, an = run(mini)
    assert dec.earliest_date_for_full_payment == date(2026, 3, 15)  # history payroll day, not the future-dated notice
    assert any(i.code == "future_message_ignored" for i in ledger.issues)
    mini.messages[0]["sent_at"] = "2026-03-05T09:30:00Z"  # same day as the request: usable
    dec, ledger, an = run(mini)
    assert dec.earliest_date_for_full_payment == date(2026, 3, 10)


def test_later_only_fx_rate_is_used_with_provenance_and_missing_rate_blocks_future_debit(mini):
    mini.profile(currency="INR", balance="100000", minimum="50000", methods="full_payment", max_months="")
    mini.monthly(15, "1000", 5, R, category="salary", event_type="income", description="International employer payroll",
                 direction="credit", currency="USD")
    mini.rate("2026-04-15", "USD", "INR", "80")  # only a LATER rate exists for the historical rows
    req(mini, amount="10000")
    dec, ledger, an = run(mini)
    prov = ledger.provenance["fx"]
    assert prov and all(p["rate"].startswith("earliest-later:2026-04-15") for p in prov)
    items = build_items(ledger, EngineConfig())
    assert [c for c in items if c.day == date(2026, 4, 15) and c.amount > 0][0].amount == Decimal("80000.00")
    # a pending debit in a currency with no rate at all can not be bounded -> conservative row
    mini.event(description="Foreign card charge", category="shopping", amount="50", currency="EUR",
               event_date="2026-03-04", settlement_date="2026-03-09", status="pending")
    dec, ledger, an = run(mini)
    assert ledger.blocked and dec.amount_safe_to_pay == 0 and dec.affordability_status == "not_affordable"
    assert any(i.code == "unknown_debit_amount" and i.severity == "blocking" for i in ledger.issues)


# ---------------------------------------------------------------- unknown debits / pending semantics

def test_unknown_future_debit_never_makes_the_answer_more_optimistic(mini):
    base(mini)
    req(mini, amount="500")
    dec, ledger, an = run(mini)
    assert dec.affordability_status == "affordable_now"
    mini.event(description="Outstanding bill", category="utilities", amount="", event_date="2026-03-04",
               settlement_date="2026-03-10", status="scheduled")  # blank amount, no image
    dec, ledger, an = run(mini)
    assert ledger.blocked
    assert (dec.amount_safe_to_pay, dec.affordability_status, dec.recommended_payment_method, dec.payment_plan) == \
        (Decimal(0), "not_affordable", "not_recommended", "none")
    assert dec.earliest_date_for_full_payment is None and "no usable amount" in dec.decision_explanation
    # an unknown HISTORICAL debit only degrades the statistics (warning), never blocks
    mini.events = [e for e in mini.events if e["description"] != "Outstanding bill"]
    mini.event(description="Old bill", category="utilities", amount="", event_date="2026-01-10", status="settled")
    dec, ledger, an = run(mini)
    assert not ledger.blocked and any(i.code == "unknown_history_amount" for i in ledger.issues)
    assert dec.affordability_status == "affordable_now"


def test_pending_debit_is_reserved_in_full_and_pending_credit_never_counts(mini):
    base(mini)
    req(mini, amount="2500")
    dec0, _, _ = run(mini)
    mini.event(description="Pending merchant debit", category="shopping", amount="300", event_date="2026-03-04",
               settlement_date="2026-03-08", status="pending")
    dec1, ledger, _ = run(mini)
    assert dec1.amount_safe_to_pay == dec0.amount_safe_to_pay - Decimal(300)  # opening balance is NOT net of pending debits
    mini.event(event_type="refund", description="Pending merchant refund", category="shopping", direction="credit",
               amount="900", event_date="2026-03-04", settlement_date="2026-03-08", status="pending")
    dec2, _, _ = run(mini)
    assert dec2.amount_safe_to_pay == dec1.amount_safe_to_pay
    # a pending debit whose settlement date is already past is reserved on the request date itself
    mini.events[-2]["settlement_date"] = "2026-03-01"
    dec3, ledger3, _ = run(mini)
    assert dec3.amount_safe_to_pay == dec1.amount_safe_to_pay
    assert any(c.day == R and c.kind == "pending" for c in ledger3.one_offs)


# ---------------------------------------------------------------- horizon / calendar

def test_four_monthly_occurrences_within_90_days_documented_behaviour(mini):
    r = date(2026, 1, 31)
    mini.profile(balance="5000", minimum="1000", methods="full_payment", max_months="")
    mini.monthly(15, "2000", 5, r, category="salary", event_type="income", description="Payroll credit", direction="credit")
    mini.monthly(1, "800", 5, r, category="rent")
    mini.request(request_date=r.isoformat(), amount="100", deadline="2026-04-30", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "100", 1, r.isoformat(), "", "0", "100")
    dec, ledger, an = run(mini)
    rent = sorted(c.day for c in build_items(ledger, EngineConfig()) if c.label.startswith("rent"))
    # calibrated behaviour (see IMPLEMENTATION_NOTES 5b): monthly templates only for Jan, Feb, Mar
    assert rent == [date(2026, 2, 1), date(2026, 3, 1)]
    # the conservative alternative projects every occurrence inside the 90 days (four of them)
    rent_days = sorted(c.day for c in build_items(ledger, EngineConfig(horizon_mode="days")) if c.label.startswith("rent"))
    assert rent_days == [date(2026, 2, 1), date(2026, 3, 1), date(2026, 4, 1), date(2026, 5, 1)]


def test_month_end_and_leap_year_clamping(mini):
    r = date(2028, 1, 5)  # 2028 is a leap year
    mini.profile(balance="5000", minimum="1000", methods="full_payment", max_months="")
    mini.monthly(15, "2000", 5, r, category="salary", event_type="income", description="Payroll credit", direction="credit")
    mini.monthly(31, "300", 4, r, category="insurance")  # 31st in months that have it (Dec 31, Oct 31, ...)
    mini.request(request_date=r.isoformat(), amount="100", deadline="2028-03-20", allows_partial="false")
    mini.option("payment_option_1", "request_1", "full_payment", "100", 1, r.isoformat(), "", "0", "100")
    dec, ledger, an = run(mini)
    ins = sorted(c.day for c in build_items(ledger, EngineConfig()) if c.label.startswith("insurance"))
    assert ins == [date(2028, 1, 31), date(2028, 2, 29), date(2028, 3, 31)]


# ---------------------------------------------------------------- partial-plan optimality

def test_partial_split_search_finds_no_better_plan_than_the_prescribed_one(mini):
    base(mini, balance="1400", minimum="1000", methods="full_payment|partial_payment")
    req(mini, amount="1000", deadline="2026-04-10", partial="true")
    dec, ledger, an = run(mini)
    assert dec.recommended_payment_method == "partial_payment"
    safe = dec.amount_safe_to_pay
    earliest = dec.earliest_date_for_full_payment
    from buyorwait.forecast import plan_is_safe

    items = build_items(ledger, EngineConfig())
    cfg = EngineConfig()
    requested = Decimal(1000)
    rng = random.Random(0)
    for _ in range(300):
        a = (Decimal(rng.randint(0, 100000)) / 100).quantize(Decimal("0.01"))
        d2 = R + timedelta(days=rng.randint(1, 60))
        ok, _low = plan_is_safe(ledger, items, [(R, a), (d2, requested - a)], cfg)
        if a > safe:
            assert not ok  # paying more than amount_safe_to_pay today is never safe
        if ok:
            assert d2 >= earliest  # no split completes before the change-free earliest date


# ---------------------------------------------------------------- scheduled vs projected duplication

def test_scheduled_rows_replace_projection_only_on_the_same_date(mini):
    base(mini)
    mini.event(event_type="income", description="Next confirmed salary", category="salary", direction="credit", amount="2000",
               event_date="2026-03-15", status="scheduled")
    req(mini, amount="100")
    dec, ledger, an = run(mini)
    credits = [(c.day, c.kind) for c in build_items(ledger, EngineConfig()) if c.amount > 0]
    assert credits.count((date(2026, 3, 15), "scheduled")) == 1 and (date(2026, 3, 15), "recurring") not in credits
    # an extra scheduled debit two days before the regular bill is an additional charge, not the bill itself
    mini.monthly(7, "100", 5, R, category="utilities", description="Utility bill")
    mini.event(description="Scheduled utility debit", category="utilities", amount="60", event_date="2026-03-05",
               settlement_date="2026-03-12", status="scheduled")
    dec, ledger, an = run(mini)
    util = sorted((c.day, str(-c.amount), c.kind) for c in build_items(ledger, EngineConfig()) if "util" in c.label.lower())
    assert (date(2026, 3, 7), "100.00", "recurring") in util and (date(2026, 3, 12), "60.00", "scheduled") in util
    # a scheduled salary that settles late (event 15th, settlement 23rd) still suppresses the projected 15th
    mini.events = [e for e in mini.events if e["description"] != "Next confirmed salary"]
    mini.event(event_type="income", description="Next confirmed salary", category="salary", direction="credit", amount="2000",
               event_date="2026-03-15", settlement_date="2026-03-23", status="scheduled")
    dec, ledger, an = run(mini)
    credits = [(c.day, c.kind) for c in build_items(ledger, EngineConfig()) if c.amount > 0 and c.day.month == 3]
    assert credits == [(date(2026, 3, 23), "scheduled")]


# ---------------------------------------------------------------- malformed input

def test_malformed_event_rows_are_explicit_and_conservative(mini, capsys):
    base(mini)
    req(mini, amount="500")
    root = mini.write()
    path = os.path.join(root, "financial_events.csv")
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["event_x1", "user_1", "expense", "Bad amount", "shopping", "debit", "12abc", "EUR", "2026-03-04", "2026-03-09", "pending", "", "fixed", ""])
        w.writerow(["event_x2", "user_1", "expense", "Bad date", "shopping", "debit", "10", "EUR", "2026-13-40", "", "settled", "", "fixed", ""])
        w.writerow(["event_x3", "user_1", "expense", "Negative", "shopping", "debit", "-5", "EUR", "2026-01-04", "", "settled", "", "fixed", ""])
    ds = load_dataset(root)
    assert any("event_x1" in w and "unparseable amount" in w for w in ds.warnings)
    assert any("event_x2" in w and "bad date" in w for w in ds.warnings)
    assert any("event_x3" in w and "negative" in w for w in ds.warnings)
    engine = Engine(ds, EngineConfig())
    dec, ledger, an = engine.decide(ds.requests[0])
    assert ledger.blocked  # the pending debit with an unparseable amount can not be bounded
    assert dec.affordability_status == "not_affordable" and dec.amount_safe_to_pay == 0


def test_malformed_request_row_gets_a_fallback_row_from_the_cli(mini, tmp_path):
    base(mini)
    req(mini, amount="500")
    root = mini.write()
    with open(os.path.join(root, "requests.csv"), "a", encoding="utf-8", newline="") as f:
        csv.writer(f, lineterminator="\n").writerow(["request_2", "user_1", "2026-03-05", "purchase", "abc", "2026-04-30", "false", "?"])
        csv.writer(f, lineterminator="\n").writerow(["request_3", "user_404", "2026-03-05", "purchase", "10", "2026-04-30", "false", "?"])
    ds = load_dataset(root)
    assert [r.request_id for r in ds.requests] == ["request_1", "request_2", "request_3"]
    assert ds.requests[1].malformed and ds.requests[2].malformed
    import main as cli  # code/main.py is importable because conftest added code/ to sys.path

    out = tmp_path / "o.csv"
    assert cli.main(["--dataset", root, "--output", str(out), "--quiet"]) == 0
    rows = list(csv.DictReader(open(out, encoding="utf-8")))
    assert [r["request_id"] for r in rows] == ["request_1", "request_2", "request_3"]
    assert rows[1]["affordability_status"] == "not_affordable" and rows[1]["amount_safe_to_pay"] == "0"
    assert rows[2]["recommended_payment_method"] == "not_recommended"


# ---------------------------------------------------------------- permutation invariance

def test_output_is_invariant_to_row_order_of_every_input_file(tmp_path):
    from test_validator_and_properties import _dataset

    root = _dataset(tmp_path, n_users=6, seed=5)
    ds = load_dataset(root)
    engine = Engine(ds, EngineConfig())
    ref = [decision_row(engine.decide(r)[0]) for r in ds.requests]
    rng = random.Random(1)
    for name in ("financial_events.csv", "messages.csv", "request_payment_options.csv", "exchange_rates.csv", "financial_profiles.csv"):
        p = os.path.join(root, name)
        with open(p, encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        body = rows[1:]
        rng.shuffle(body)
        with open(p, "w", encoding="utf-8", newline="") as f:
            csv.writer(f, lineterminator="\n").writerows([rows[0]] + body)
    ds2 = load_dataset(root)
    engine2 = Engine(ds2, EngineConfig())
    assert [decision_row(engine2.decide(r)[0]) for r in ds2.requests] == ref


# ---------------------------------------------------------------- image evidence validation

def test_image_cache_entries_are_validated_not_trusted(mini, tmp_path):
    base(mini)
    ev = mini.event(description="Outstanding rent balance", category="rent", amount="", event_date="2026-03-04",
                    settlement_date="2026-03-09", status="scheduled")
    mini.image("image_x", ev)
    req(mini, amount="500")
    root = mini.write()
    png = b"\x89PNG\r\n\x1a\n" + b"synthetic-2"
    with open(f"{root}/media/images/image_x.png", "wb") as f:
        f.write(png)
    h = hashlib.sha256(png).hexdigest()
    ds = load_dataset(root)

    def run_with(entry):
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps({h: entry}))
        engine = Engine(ds, EngineConfig(), ImageExtractor(cache_path=str(cache), enable_vlm=False))
        return engine.decide(ds.requests[0])

    good = {"image_id": "image_x", "event_id": ev, "amount": "250", "currency": "EUR", "confidence": 0.9, "method": "test"}
    dec, ledger, _ = run_with(good)
    assert not ledger.blocked and any(c.amount == Decimal("-250.00") for c in ledger.one_offs)
    for bad, why in [({**good, "currency": "USD"}, "currency"), ({**good, "event_id": "event_other"}, "event id"),
                     ({**good, "confidence": 0.2}, "confidence"), ({**good, "amount": "-5"}, "negative"),
                     ({**good, "amount": "lots"}, "non-numeric")]:
        dec, ledger, _ = run_with(bad)
        assert ledger.blocked and dec.amount_safe_to_pay == 0, why


# ---------------------------------------------------------------- monotone safety properties

def _decide(mini):
    dec, ledger, an = run(mini)
    return dec


def _rank(status):
    return {"affordable_now": 3, "affordable_with_plan": 2, "affordable_later": 1, "not_affordable": 0}[status]


def test_adding_a_debit_never_increases_safe_amount_or_improves_status(tmp_path):
    rng = random.Random(9)
    for k in range(12):
        mini = MiniData(str(tmp_path / f"d{k}"))
        base(mini, balance=str(rng.randint(1200, 4000)), minimum="1000", methods="full_payment|partial_payment|installments",
             stop="streaming", reduce="dining")
        mini.request(amount=str(rng.randint(200, 2500)), deadline="2026-04-30", allows_partial=rng.choice(["true", "false"]))
        mini.option("payment_option_1", "request_1", "full_payment", mini.requests[0]["requested_amount"], 1, R.isoformat(), "", "0",
                    mini.requests[0]["requested_amount"])
        before = _decide(mini)
        mini.event(description="Extra pending charge", category="shopping", amount=str(rng.randint(10, 600)),
                   event_date="2026-03-04", settlement_date=(R + timedelta(days=rng.randint(0, 40))).isoformat(), status="pending")
        after = _decide(mini)
        assert after.amount_safe_to_pay <= before.amount_safe_to_pay
        assert _rank(after.affordability_status) <= _rank(before.affordability_status)
        if before.earliest_date_for_full_payment and after.earliest_date_for_full_payment:
            assert after.earliest_date_for_full_payment >= before.earliest_date_for_full_payment


def test_removing_income_never_improves_the_recommendation(tmp_path):
    rng = random.Random(4)
    for k in range(10):
        mini = MiniData(str(tmp_path / f"i{k}"))
        base(mini, balance=str(rng.randint(1200, 4000)), minimum="1000", methods="full_payment|partial_payment")
        mini.request(amount=str(rng.randint(500, 3000)), deadline="2026-05-20", allows_partial="true")
        mini.option("payment_option_1", "request_1", "full_payment", mini.requests[0]["requested_amount"], 1, R.isoformat(), "", "0",
                    mini.requests[0]["requested_amount"])
        before = _decide(mini)
        mini.message("Your employment has ended. There are no regular salary payments scheduled after the final settlement.")
        after = _decide(mini)
        assert after.amount_safe_to_pay <= before.amount_safe_to_pay
        assert _rank(after.affordability_status) <= _rank(before.affordability_status)
        assert after.earliest_date_for_full_payment is None or (
            before.earliest_date_for_full_payment is not None and after.earliest_date_for_full_payment >= before.earliest_date_for_full_payment)


@pytest.mark.skipif(not os.path.exists(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "dataset", "requests.csv")),
                    reason="real dataset not present")
def test_real_dataset_has_no_blocked_or_failed_requests():
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "dataset")
    ds = load_dataset(root)
    assert not ds.warnings
    engine = Engine(ds, EngineConfig())
    blocked = [r.request_id for r in ds.requests if engine.decide(r)[1].blocked]
    assert blocked == []
