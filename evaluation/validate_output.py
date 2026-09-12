"""Validate an output.csv against the Buy or Wait? contract.

Usage (from repo root):
    python evaluation/validate_output.py [--output output.csv] [--dataset dataset] [--requests dataset/requests.csv]

Checks: exact header/order, one row per request (no duplicates/missing), enum values, date and
payment-plan grammar, numeric bounds, partial-plan exactness, instalment correspondence to a supplied
option, spending-change legality, internal consistency, and a re-simulation of every recommended plan
(with its spending changes) against the 90-day minimum-balance rule using the deterministic engine.
Exit code 0 when valid, 1 otherwise.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "code"))

from buyorwait.config import EngineConfig  # noqa: E402
from buyorwait.forecast import build_items  # noqa: E402
from buyorwait.images import ImageExtractor  # noqa: E402
from buyorwait.ingest import OUTPUT_COLUMNS, load_dataset  # noqa: E402
from buyorwait.ledger import LedgerBuilder  # noqa: E402
from buyorwait.models import SpendingChange  # noqa: E402

STATUSES = {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
METHODS = {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
CENT = Decimal("0.01")


def parse_plan(text: str):
    if text == "none":
        return []
    out = []
    for part in text.split("|"):
        if ":" not in part:
            raise ValueError(f"bad plan entry {part!r}")
        d, a = part.split(":", 1)
        if not DATE_RE.match(d) or not AMOUNT_RE.match(a):
            raise ValueError(f"bad plan entry {part!r}")
        out.append((date.fromisoformat(d), Decimal(a)))
    return out


def parse_changes(text: str):
    if text == "none":
        return []
    out = []
    for part in text.split("|"):
        bits = part.split(":")
        if bits[0] == "stop" and len(bits) == 2:
            out.append(("stop", bits[1], None))
        elif bits[0] == "reduce_to" and len(bits) == 3 and AMOUNT_RE.match(bits[2]):
            out.append(("reduce_to", bits[1], Decimal(bits[2])))
        else:
            raise ValueError(f"bad spending change {part!r}")
    return out


def _num_tokens(text: str) -> set[str]:
    """Numbers mentioned in an explanation, normalised (no thousands separators, no trailing .00)."""
    out = set()
    for tok in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        t = tok.replace(",", "")
        try:
            out.add(str(Decimal(t).quantize(CENT).normalize()))
        except InvalidOperation:
            continue
    return out


def _norm(x: Decimal) -> str:
    return str(Decimal(x).quantize(CENT).normalize())


def explanation_consistency(d: dict, req, prof) -> list[str]:
    """The explanation must be consistent with the structured fields it justifies."""
    text = d["decision_explanation"]
    low = text.lower()
    nums = _num_tokens(text)
    method = d["recommended_payment_method"]
    errs = []
    if len(text) < 40:
        errs.append("explanation too short to be useful")
    if method == "not_recommended":
        if not any(w in low for w in ("do not", "not safe", "cannot", "no payment option")):
            errs.append("not_recommended explanation must say the payment should not proceed")
    if method == "wait":
        try:
            plan_date = date.fromisoformat(d["payment_plan"].split(":")[0])
            if str(plan_date.day) not in text or plan_date.strftime("%B") not in text:
                errs.append("wait explanation must mention the payment date")
        except ValueError:
            pass
        if "wait" not in low and "in full on" not in low:
            errs.append("wait explanation must say to wait / pay later")
    if method == "installments" and "installment" not in low:
        errs.append("installments explanation must mention installments")
    if method == "partial_payment":
        parts = d["payment_plan"].split("|")
        for part in parts:
            amt = part.split(":")[1]
            if _norm(Decimal(amt)) not in nums:
                errs.append(f"partial explanation must quote the payment amount {amt}")
    if method == "full_payment" and _norm(req.requested_amount) not in nums:
        errs.append("full_payment explanation must quote the requested amount")
    if d["spending_changes_needed"] != "none" and not any(w in low for w in ("stop", "reduce")):
        errs.append("explanation must mention the required spending change")
    if prof is not None and _norm(prof.minimum_balance_to_keep) not in nums and not (
            method == "not_recommended" and ("safely" in low or "90 days" in low)):
        errs.append("explanation must state the minimum balance constraint")
    return errs


def independent_walk(opening: Decimal, items, payments, start: date, end: date) -> Decimal:
    """Plain daily balance walk: opening balance, then every item/payment on its day; returns the lowest
    end-of-day balance in [start, end]. Deliberately written without the engine's Path/low machinery."""
    per_day: dict[date, Decimal] = {}
    for c in items:
        if start <= c.day <= end:
            per_day[c.day] = per_day.get(c.day, Decimal(0)) + c.amount
    for d, a in payments:
        if start <= d <= end:
            per_day[d] = per_day.get(d, Decimal(0)) - a
    bal = opening
    low = opening
    d = start
    one = timedelta(days=1)
    while d <= end:
        bal += per_day.get(d, Decimal(0))
        low = min(low, bal)
        d += one
    return low


def independent_baseline(ledger, items, requested: Decimal) -> tuple[Decimal, date | None]:
    """Change-free amount_safe_to_pay and earliest full-payment date from a plain daily walk."""
    per_day: dict[date, Decimal] = {}
    for c in items:
        if ledger.start <= c.day <= ledger.end:
            per_day[c.day] = per_day.get(c.day, Decimal(0)) + c.amount
    days = []
    bal = ledger.balance
    d = ledger.start
    one = timedelta(days=1)
    while d <= ledger.end:
        bal += per_day.get(d, Decimal(0))
        days.append((d, bal))
        d += one
    low = min(b for _, b in days)
    safe = max(Decimal(0), min(requested, low - ledger.minimum)).quantize(CENT)
    earliest = None
    for i, (d, _) in enumerate(days):
        if any(b < ledger.minimum for _, b in days[:i]):
            break
        if all(b - requested >= ledger.minimum for _, b in days[i:]):
            earliest = d
            break
    return safe, earliest


def validate(output_path: str, dataset_dir: str, requests_path: str | None = None, simulate: bool = True) -> list[str]:
    errors: list[str] = []
    ds = load_dataset(dataset_dir)
    requests = ds.requests
    if requests_path:
        from buyorwait.ingest import parse_bool, parse_date, parse_decimal
        from buyorwait.models import Request

        with open(requests_path, encoding="utf-8-sig", newline="") as f:
            requests = [
                Request(r["request_id"], r["user_id"], parse_date(r["request_date"]), r.get("request_type", ""),
                        parse_decimal(r["requested_amount"]), parse_date(r["desired_completion_date"]),
                        parse_bool(r.get("allows_partial_payment", "")), r.get("request_text", ""))
                for r in csv.DictReader(f)
            ]
    req_by_id = {r.request_id: r for r in requests}

    with open(output_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return ["output is empty"]
        rows = list(reader)
    if header != OUTPUT_COLUMNS:
        errors.append(f"header mismatch: {header} != {OUTPUT_COLUMNS}")
        return errors
    seen = set()
    dict_rows = []
    for i, r in enumerate(rows, start=2):
        if len(r) != len(OUTPUT_COLUMNS):
            errors.append(f"line {i}: expected {len(OUTPUT_COLUMNS)} columns, got {len(r)}")
            continue
        d = dict(zip(OUTPUT_COLUMNS, r, strict=True))
        if d["request_id"] in seen:
            errors.append(f"line {i}: duplicate request_id {d['request_id']}")
        seen.add(d["request_id"])
        dict_rows.append((i, d))
    missing = [rid for rid in req_by_id if rid not in seen]
    if missing:
        errors.append(f"missing predictions for {len(missing)} requests: {missing[:5]}...")
    extra = [rid for rid in seen if rid not in req_by_id]
    if extra:
        errors.append(f"unknown request_ids: {extra[:5]}...")

    builder = LedgerBuilder(ds, ImageExtractor(), EngineConfig()) if simulate else None
    cfg = EngineConfig()

    for i, d in dict_rows:
        rid = d["request_id"]
        req = req_by_id.get(rid)
        if req is None:
            continue
        prof = ds.profiles.get(req.user_id)
        pre = f"{rid} (line {i})"
        # enums
        if d["affordability_status"] not in STATUSES:
            errors.append(f"{pre}: bad affordability_status {d['affordability_status']!r}")
        if d["recommended_payment_method"] not in METHODS:
            errors.append(f"{pre}: bad recommended_payment_method {d['recommended_payment_method']!r}")
        # amount bounds
        try:
            safe = Decimal(d["amount_safe_to_pay"])
        except InvalidOperation:
            errors.append(f"{pre}: amount_safe_to_pay not numeric")
            continue
        if not (Decimal(0) <= safe <= req.requested_amount):
            errors.append(f"{pre}: amount_safe_to_pay {safe} outside [0, {req.requested_amount}]")
        # dates / grammar
        earliest = d["earliest_date_for_full_payment"]
        if earliest and not DATE_RE.match(earliest):
            errors.append(f"{pre}: bad earliest_date_for_full_payment {earliest!r}")
        try:
            plan = parse_plan(d["payment_plan"])
        except ValueError as exc:
            errors.append(f"{pre}: {exc}")
            plan = None
        try:
            changes = parse_changes(d["spending_changes_needed"])
        except ValueError as exc:
            errors.append(f"{pre}: {exc}")
            changes = None
        if not d["decision_explanation"].strip():
            errors.append(f"{pre}: empty decision_explanation")
        status, method = d["affordability_status"], d["recommended_payment_method"]
        errors.extend(f"{pre}: {e}" for e in explanation_consistency(d, req, prof))
        if plan is None or changes is None:
            continue
        # chronological order, dates inside forecast, amounts positive
        if any(a <= 0 for _, a in plan):
            errors.append(f"{pre}: non-positive payment amount")
        if any(plan[k][0] > plan[k + 1][0] for k in range(len(plan) - 1)):
            errors.append(f"{pre}: payment_plan not chronological")
        if plan and plan[0][0] < req.request_date:
            errors.append(f"{pre}: payment before request_date")
        # consistency between status / method / plan / earliest
        if status == "affordable_now":
            if earliest != req.request_date.isoformat():
                errors.append(f"{pre}: affordable_now requires earliest_date == request_date")
            if method != "full_payment" or changes:
                errors.append(f"{pre}: affordable_now requires full_payment without spending changes")
            if safe != req.requested_amount.quantize(CENT) and safe != req.requested_amount:
                errors.append(f"{pre}: affordable_now requires amount_safe_to_pay == requested_amount")
        if method == "not_recommended":
            if status != "not_affordable" or plan or changes:
                errors.append(f"{pre}: not_recommended must have not_affordable, plan none and changes none")
        elif status == "not_affordable":
            errors.append(f"{pre}: not_affordable must use not_recommended")
        if method == "wait":
            if status != "affordable_later" or len(plan) != 1 or changes:
                errors.append(f"{pre}: wait requires affordable_later, one payment and no changes")
            elif plan[0][1] != req.requested_amount or plan[0][0].isoformat() != earliest or plan[0][0] <= req.request_date:
                errors.append(f"{pre}: wait plan must be one full payment on earliest_date after request_date")
        if method == "full_payment":
            if len(plan) != 1 or plan[0][0] != req.request_date or plan[0][1] != req.requested_amount:
                errors.append(f"{pre}: full_payment plan must be requested_amount on request_date")
            if changes and status != "affordable_with_plan":
                errors.append(f"{pre}: full_payment with changes must be affordable_with_plan")
        if method == "partial_payment":
            if status != "affordable_with_plan":
                errors.append(f"{pre}: partial_payment must be affordable_with_plan")
            if not req.allows_partial_payment:
                errors.append(f"{pre}: request does not allow partial payment")
            if prof and "partial_payment" not in prof.methods:
                errors.append(f"{pre}: user does not consider partial_payment")
            if not (Decimal(0) < safe < req.requested_amount):
                errors.append(f"{pre}: partial requires 0 < amount_safe_to_pay < requested_amount")
            if len(plan) != 2:
                errors.append(f"{pre}: partial plan must have exactly two payments")
            else:
                (d1, a1), (d2, a2) = plan
                if d1 != req.request_date or a1 != safe:
                    errors.append(f"{pre}: first partial payment must be amount_safe_to_pay on request_date")
                if d2.isoformat() != earliest or a2 != (req.requested_amount - safe):
                    errors.append(f"{pre}: second partial payment must be the remainder on earliest_date")
                if a1 + a2 != req.requested_amount:
                    errors.append(f"{pre}: partial payments do not add up to requested_amount")
                if d2 > req.desired_completion_date:
                    errors.append(f"{pre}: partial completion after desired_completion_date")
        if method == "installments":
            if status != "affordable_with_plan":
                errors.append(f"{pre}: installments must be affordable_with_plan")
            if prof and "installments" not in prof.methods:
                errors.append(f"{pre}: user does not consider installments")
            opts = [o for o in ds.options_by_request.get(rid, []) if o.payment_method == "installments"]
            match = None
            for o in opts:
                if [(dd, aa.quantize(CENT)) for dd, aa in o.schedule()] == [(dd, aa.quantize(CENT)) for dd, aa in plan]:
                    match = o
                    break
            if match is None:
                errors.append(f"{pre}: installment plan does not match any supplied option")
            elif prof and prof.max_installment_months is not None and match.number_of_payments > prof.max_installment_months:
                errors.append(f"{pre}: option {match.payment_option_id} exceeds max_installment_months")
        if method in ("full_payment", "partial_payment", "installments", "wait") and prof and method != "wait" and method not in prof.methods:
            errors.append(f"{pre}: method {method} not in payment_methods_user_will_consider")
        if method == "wait" and prof and "full_payment" not in prof.methods:
            errors.append(f"{pre}: wait requires the user to consider full_payment")
        # spending-change legality
        if len(changes) > 3:
            errors.append(f"{pre}: more than three spending changes")
        ids = [c[1] for c in changes]
        if len(set(ids)) != len(ids):
            errors.append(f"{pre}: the same event appears twice in spending changes")
        for action, eid, amt in changes:
            ev = ds.events_by_id.get(eid)
            if ev is None or ev.user_id != req.user_id:
                errors.append(f"{pre}: change references unknown/foreign event {eid}")
                continue
            if ev.direction != "debit" or not ev.is_flexible:
                errors.append(f"{pre}: {eid} is not a flexible recurring expense")
            if prof and ev.category in prof.protect:
                errors.append(f"{pre}: {eid} is in a protected category")
            if action == "stop":
                if not ev.can_stop or (prof and ev.category not in prof.stop):
                    errors.append(f"{pre}: user does not permit stopping {eid} ({ev.category})")
            else:
                if not ev.can_reduce or (prof and ev.category not in prof.reduce):
                    errors.append(f"{pre}: user does not permit reducing {eid} ({ev.category})")
                elif ev.minimum_allowed_amount is not None and amt < ev.minimum_allowed_amount.quantize(CENT):
                    errors.append(f"{pre}: reduce_to amount {amt} below minimum_allowed_amount {ev.minimum_allowed_amount}")
                elif ev.amount is not None and amt >= ev.amount:
                    errors.append(f"{pre}: reduce_to amount {amt} is not a reduction of {ev.amount}")
        # re-simulation of the recommended plan with an INDEPENDENT day-by-day walk (the engine's forecast items
        # are reused, but none of its balance/safety arithmetic), plus an independent recomputation of the
        # change-free amount_safe_to_pay and earliest_date_for_full_payment from those items.
        if simulate and builder is not None:
            ledger = builder.build(req)
            if ledger.blocked:
                if method != "not_recommended" or safe != 0:
                    errors.append(f"{pre}: unknown future debit(s) in the ledger but a non-conservative row was produced")
                continue
            base_items = build_items(ledger, cfg)
            ind_safe, ind_earliest = independent_baseline(ledger, base_items, req.requested_amount)
            if abs(ind_safe - safe) > CENT:
                errors.append(f"{pre}: amount_safe_to_pay {safe} != independently recomputed {ind_safe}")
            ind_e = ind_earliest.isoformat() if ind_earliest else ""
            if ind_e != earliest and not (status == "affordable_now" and ind_e == req.request_date.isoformat()):
                errors.append(f"{pre}: earliest_date_for_full_payment {earliest!r} != independently recomputed {ind_e!r}")
            if plan and method != "not_recommended":
                key_by_event = {s.last_event_id: s for s in ledger.series}
                sc = []
                for action, eid, amt in changes:
                    s = key_by_event.get(eid)
                    if s is None:
                        for cand in ledger.series:
                            if any(o.event_id == eid for o in cand.occurrences):
                                s = cand
                                break
                    if s is None:
                        errors.append(f"{pre}: change {eid} does not map to an active recurring series")
                        continue
                    sc.append(SpendingChange(action, eid, s.key, amt))
                items = build_items(ledger, cfg, sc)
                low = independent_walk(ledger.balance, items, plan, ledger.start, ledger.end)
                if low < ledger.minimum:
                    errors.append(f"{pre}: re-simulated plan breaches minimum balance (low {low} < {ledger.minimum})")
                if any(d > ledger.end for d, _ in plan):
                    errors.append(f"{pre}: plan has a payment after the forecast window")
    return errors


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--output", default=os.path.join(ROOT, "output.csv"))
    p.add_argument("--dataset", default=os.path.join(ROOT, "dataset"))
    p.add_argument("--requests", default=None, help="requests CSV the output answers (default: <dataset>/requests.csv)")
    p.add_argument("--no-simulate", action="store_true", help="skip plan re-simulation")
    args = p.parse_args(argv)
    errors = validate(args.output, args.dataset, args.requests, simulate=not args.no_simulate)
    if errors:
        print(f"INVALID: {len(errors)} problem(s)")
        for e in errors[:200]:
            print("  -", e)
        return 1
    print("VALID: output passes all checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
