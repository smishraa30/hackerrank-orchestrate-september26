"""Validator self-tests (deliberately broken outputs must be rejected), engine properties
(bounds, safety invariant, monotonicity, determinism) and a sample-metrics regression guard."""
from __future__ import annotations

import csv
import os
import random
import sys
from datetime import date, timedelta
from decimal import Decimal

import pytest
from conftest import ROOT, MiniData

sys.path.insert(0, os.path.join(ROOT, "evaluation"))
from buyorwait.config import EngineConfig  # noqa: E402
from buyorwait.ingest import OUTPUT_COLUMNS, load_dataset  # noqa: E402
from buyorwait.pipeline import Engine  # noqa: E402
from buyorwait.render import decision_row  # noqa: E402
from validate_output import validate  # noqa: E402

R = date(2026, 3, 5)


def _dataset(tmp_path, n_users=6, seed=1):
    """Random-but-plausible users: fixed salary, rent, a couple of interval series, some flexible items."""
    rng = random.Random(seed)
    md = MiniData(str(tmp_path / "dataset"))
    for i in range(1, n_users + 1):
        uid = f"user_{i}"
        methods = rng.choice(["full_payment", "full_payment|partial_payment", "installments", "partial_payment|installments",
                              "full_payment|partial_payment|installments"])
        mx = "" if "installments" not in methods else str(rng.choice([2, 3, 6]))
        md.profile(user_id=uid, balance=str(rng.randint(800, 6000)), minimum=str(rng.randint(300, 1500)), methods=methods, max_months=mx,
                   reduce="dining", stop="streaming", protect="rent")
        md.monthly(15, str(rng.choice([1500, 2000, 2600])), 5, R, category="salary", event_type="income", description="Payroll credit",
                   direction="credit", user_id=uid)
        md.monthly(2, str(rng.choice([600, 800, 1100])), 5, R, category="rent", user_id=uid)
        md.monthly(10, "40", 5, R, category="streaming", event_type="subscription", description="Family streaming plan",
                   flexibility="stoppable", user_id=uid)
        md.interval(7, [str(rng.randint(40, 70)) for _ in range(4)], R, category="groceries", offset=rng.randint(2, 6), user_id=uid)
        md.interval(14, [str(rng.randint(50, 90)) for _ in range(3)], R, category="dining", offset=rng.randint(2, 6), user_id=uid,
                    flexibility="reducible", min_allowed="30")
        amount = rng.randint(200, 3000)
        rid = f"request_{i}"
        md.request(request_id=rid, user_id=uid, amount=str(amount), deadline=(R + timedelta(days=rng.randint(7, 80))).isoformat(),
                   allows_partial=rng.choice(["true", "false"]))
        md.option(f"payment_option_{i}a", rid, "full_payment", str(amount), 1, R.isoformat(), "", "0", str(amount))
        n = rng.choice([2, 3, 6])
        per = (Decimal(amount) * Decimal("1.05") / n).quantize(Decimal("0.01"))
        md.option(f"payment_option_{i}b", rid, "installments", str(per), n, (R + timedelta(days=3)).isoformat(), "30",
                  str(per * n - amount), str(per * n))
    return md.write()


def _decisions(root):
    ds = load_dataset(root)
    engine = Engine(ds, EngineConfig())
    return ds, [engine.decide(r)[0] for r in ds.requests]


def _write(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_validator_accepts_engine_output_and_rejects_corruptions(tmp_path):
    root = _dataset(tmp_path, n_users=8, seed=7)
    ds, decs = _decisions(root)
    rows = [decision_row(d) for d in decs]
    out = tmp_path / "out.csv"
    _write(out, rows)
    assert validate(str(out), root) == []

    def corrupt(mutate, expect):
        bad = [dict(r) for r in rows]
        mutate(bad)
        p = tmp_path / "bad.csv"
        _write(p, bad)
        errs = validate(str(p), root)
        assert errs and any(expect in e for e in errs), (expect, errs)

    corrupt(lambda b: b.append(dict(b[0])), "duplicate request_id")
    corrupt(lambda b: b.pop(), "missing predictions")
    corrupt(lambda b: b[0].update(affordability_status="maybe"), "bad affordability_status")
    corrupt(lambda b: b[0].update(recommended_payment_method="loan"), "bad recommended_payment_method")
    corrupt(lambda b: b[0].update(amount_safe_to_pay="-1"), "outside")
    corrupt(lambda b: b[0].update(amount_safe_to_pay="999999999"), "outside")
    corrupt(lambda b: b[0].update(payment_plan="2026/03/05:100"), "bad plan entry")
    corrupt(lambda b: b[0].update(spending_changes_needed="stop:event_1:5"), "bad spending change")
    corrupt(lambda b: b[0].update(decision_explanation=""), "empty decision_explanation")
    # an instalment plan that matches no supplied option
    for r in rows:
        if r["recommended_payment_method"] != "not_recommended":
            break
    corrupt(lambda b: [x.update(affordability_status="affordable_with_plan", recommended_payment_method="installments",
                                payment_plan="2026-03-08:1|2026-04-07:1", spending_changes_needed="none")
                       for x in b if x["request_id"] == r["request_id"]], "does not match any supplied option")
    # affordable_now must carry earliest == request_date and a full payment plan
    corrupt(lambda b: [x.update(affordability_status="affordable_now", recommended_payment_method="full_payment",
                                payment_plan="2026-03-05:1", earliest_date_for_full_payment="2026-04-01", spending_changes_needed="none")
                       for x in b if x["request_id"] == r["request_id"]], "affordable_now")
    # header order
    p = tmp_path / "hdr.csv"
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(reversed(OUTPUT_COLUMNS)), lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    assert any("header mismatch" in e for e in validate(str(p), root))


def test_validator_rejects_unsafe_plan_and_illegal_change(tmp_path):
    md = MiniData(str(tmp_path / "dataset"))
    md.profile(balance="1300", minimum="1000", methods="full_payment", max_months="", reduce="dining", stop="streaming", protect="rent")
    md.monthly(15, "2000", 5, R, category="salary", event_type="income", description="Payroll credit", direction="credit")
    rent_ids = md.monthly(2, "800", 5, R, category="rent")
    md.request(amount="200", deadline="2026-04-30", allows_partial="false")
    md.option("payment_option_1", "request_1", "full_payment", "200", 1, R.isoformat(), "", "0", "200")
    root = md.write()
    unsafe = [{"request_id": "request_1", "amount_safe_to_pay": "300", "affordability_status": "affordable_now",
               "recommended_payment_method": "full_payment", "payment_plan": "2026-03-05:200",
               "earliest_date_for_full_payment": "2026-03-05", "spending_changes_needed": "none", "decision_explanation": "x"}]
    # claim 300 is safe when only 300 headroom exists and the request is 200 -> safe today, but a wrong safe amount
    unsafe[0]["amount_safe_to_pay"] = "500"
    p = tmp_path / "u.csv"
    _write(p, unsafe)
    errs = validate(str(p), root)
    assert any("outside" in e for e in errs)  # 500 > requested 200
    # illegal change on a protected fixed category
    row = dict(unsafe[0], amount_safe_to_pay="200", affordability_status="affordable_with_plan",
               spending_changes_needed=f"stop:{rent_ids[-1]}")
    _write(p, [row])
    errs = validate(str(p), root)
    assert any("not a flexible" in e or "protected" in e for e in errs)
    # a plan that breaches the minimum (pay 400 today with 300 headroom)
    md.requests[0]["requested_amount"] = "400"
    md.options[0]["payment_amount"] = md.options[0]["total_payable_amount"] = "400"
    root = md.write()
    row = dict(unsafe[0], amount_safe_to_pay="300", affordability_status="affordable_later", recommended_payment_method="wait",
               payment_plan="2026-03-06:400", earliest_date_for_full_payment="2026-03-06", spending_changes_needed="none")
    _write(p, [row])
    errs = validate(str(p), root)
    assert any("breaches minimum balance" in e for e in errs)


def test_engine_properties_bounds_safety_and_determinism(tmp_path):
    root = _dataset(tmp_path, n_users=12, seed=3)
    ds, decs = _decisions(root)
    rows = [decision_row(d) for d in decs]
    out = tmp_path / "out.csv"
    _write(out, rows)
    assert validate(str(out), root) == []  # every recommended plan re-simulates safe; all fields legal
    for d, req in zip(decs, ds.requests, strict=True):
        assert Decimal(0) <= d.amount_safe_to_pay <= req.requested_amount
        if d.affordability_status == "affordable_now":
            assert d.earliest_date_for_full_payment == req.request_date
    # determinism: a second engine instance yields byte-identical rows
    ds2, decs2 = _decisions(root)
    assert [decision_row(d) for d in decs2] == rows


def test_amount_safe_is_monotone_in_opening_balance(tmp_path):
    root = _dataset(tmp_path, n_users=5, seed=11)
    ds = load_dataset(root)
    prev = None
    for bump in (Decimal(0), Decimal(200), Decimal(1000)):
        eng = Engine(ds, EngineConfig())
        vals = []
        for req in ds.requests:
            prof = ds.profiles[req.user_id]
            ds.profiles[req.user_id] = prof.__class__(**{**prof.__dict__, "current_available_balance": prof.current_available_balance + bump})
            dec, _, _ = eng.decide(req)
            ds.profiles[req.user_id] = prof
            vals.append(dec.amount_safe_to_pay)
        if prev is not None:
            assert all(v >= p for v, p in zip(vals, prev, strict=True))
        prev = vals


REAL = os.path.join(ROOT, "dataset", "sample_requests.csv")


@pytest.mark.skipif(not os.path.exists(REAL), reason="real dataset not present")
def test_sample_metrics_do_not_regress():
    from evaluate_samples import evaluate

    rep = evaluate(EngineConfig(), os.path.join(ROOT, "dataset"), tol=0.05)
    assert rep["exact"]["affordability_status"] >= 0.92
    assert rep["exact"]["recommended_payment_method"] >= 0.92
    assert rep["exact"]["earliest_date_for_full_payment"] >= 0.92
    assert rep["exact"]["spending_changes_needed"] >= 0.88
    assert rep["within_tol"]["amount_safe_to_pay"] >= 0.84
