"""Payment-candidate generation, safety validation and ranking."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from .config import EngineConfig
from .forecast import amount_safe_today, balance_path, build_items, earliest_full_payment_date, plan_is_safe, series_occurrences
from .fx import CENT
from .ledger import Ledger
from .models import PaymentOption, Plan, Request, Series, SpendingChange


@dataclass
class Analysis:
    amount_safe: Decimal
    earliest: Optional[date]
    baseline_low: Decimal
    baseline_low_date: Optional[date]
    candidates: list[Plan] = field(default_factory=list)
    chosen: Optional[Plan] = None
    rejected: list[str] = field(default_factory=list)
    change_options: list[dict] = field(default_factory=list)
    blocked_reason: Optional[str] = None


def rank_key(p: Plan):
    return (
        0 if p.completes_by_deadline else 1,
        1 if p.changes else 0,
        p.total_paid,
        p.start_date or date.max,
        p.n_payments,
        p.option_number,
        p.option_id or "",
    )


def _total(payments: list[tuple[date, Decimal]]) -> Decimal:
    return sum((a for _, a in payments), Decimal(0)).quantize(CENT)


def change_reduction(ledger: Ledger, s: Series, action: str, cfg: EngineConfig) -> Decimal:
    """Total reduction over the window produced by one change."""
    from .forecast import series_amount_on

    total = Decimal(0)
    for d in series_occurrences(s, ledger.start, ledger.end, cfg):
        amt = series_amount_on(s, d, ledger)
        if action == "stop":
            total += amt
        else:
            total += max(Decimal(0), amt - (s.minimum_allowed_amount or Decimal(0)))
    return total


def enumerate_changes(ledger: Ledger, cfg: EngineConfig, max_changes: int = 3) -> list[list[SpendingChange]]:
    """All legal combinations (up to three series, one action each), smallest reduction first."""
    p = ledger.profile
    singles: list[tuple[Decimal, SpendingChange]] = []
    for s in ledger.flexible_series():
        if s.can_stop and s.category in p.stop:
            ch = SpendingChange("stop", s.last_event_id, s.key)
            singles.append((change_reduction(ledger, s, "stop", cfg), ch))
        if s.can_reduce and s.category in p.reduce and s.minimum_allowed_amount is not None:
            ch = SpendingChange("reduce_to", s.last_event_id, s.key, s.minimum_allowed_amount.quantize(CENT))
            singles.append((change_reduction(ledger, s, "reduce_to", cfg), ch))
    combos: list[tuple[Decimal, int, list[SpendingChange]]] = []
    for k in range(1, max_changes + 1):
        for combo in itertools.combinations(singles, k):
            keys = [c[1].series_key for c in combo]
            if len(set(keys)) != len(keys):
                continue  # stop and reduce on the same series are mutually exclusive
            red = sum((c[0] for c in combo), Decimal(0))
            combos.append((red, k, [c[1] for c in combo]))
    combos.sort(key=lambda t: (t[0], t[1], [c.event_id for c in t[2]]))
    return [c[2] for c in combos]


def eligible_options(req: Request, options: list[PaymentOption], ledger: Ledger, rejected: list[str]) -> list[PaymentOption]:
    prof = ledger.profile
    out = []
    for o in options:
        if o.payment_method != "installments":
            continue
        if "installments" not in prof.methods:
            rejected.append(f"{o.payment_option_id}: user does not consider installments")
            continue
        if prof.max_installment_months is not None and o.number_of_payments > prof.max_installment_months:
            rejected.append(f"{o.payment_option_id}: {o.number_of_payments} payments exceed max_installment_months={prof.max_installment_months}")
            continue
        sched = o.schedule()
        if sched[-1][0] > ledger.end:
            rejected.append(f"{o.payment_option_id}: last payment {sched[-1][0]} is outside the 90-day forecast")
            continue
        if sched[0][0] < req.request_date:
            rejected.append(f"{o.payment_option_id}: first payment before request date")
            continue
        out.append(o)
    return out


def analyse(req: Request, ledger: Ledger, options: list[PaymentOption], cfg: EngineConfig) -> Analysis:
    prof = ledger.profile
    requested = req.requested_amount.quantize(CENT)
    if ledger.blocked:
        # An unknown future debit means no balance path can be bounded: the only safe answer is the conservative one.
        reasons = "; ".join(i.detail for i in ledger.issues if i.severity == "blocking")
        an = Analysis(amount_safe=Decimal(0), earliest=None, baseline_low=ledger.balance, baseline_low_date=ledger.start,
                      blocked_reason=reasons)
        an.rejected.append(f"all plans: forecast blocked by unknown future debit(s): {reasons}")
        return an
    base_items = build_items(ledger, cfg)
    base_path = balance_path(ledger, base_items, cfg)
    safe = amount_safe_today(ledger, base_path, requested)
    earliest = earliest_full_payment_date(ledger, base_path, requested, cfg, base_items)
    low = min(base_path.lows)
    low_date = base_path.days[base_path.lows.index(low)]
    an = Analysis(amount_safe=safe, earliest=earliest, baseline_low=low, baseline_low_date=low_date)
    deadline = req.desired_completion_date
    cands: list[Plan] = []

    # full payment today / wait
    if "full_payment" in prof.methods:
        if safe >= requested and earliest == req.request_date:
            cands.append(Plan("full_payment", [(req.request_date, requested)], [], completes_by_deadline=True,
                              total_paid=requested, safe=True))
        elif earliest is not None:
            cands.append(Plan("wait", [(earliest, requested)], [], completes_by_deadline=earliest <= deadline,
                              total_paid=requested, safe=True))
    else:
        an.rejected.append("full_payment/wait: user does not consider full_payment")

    # partial payment: exactly two payments, second on the earliest full-payment date
    if "partial_payment" in prof.methods and req.allows_partial_payment:
        if Decimal(0) < safe < requested and earliest is not None and earliest <= deadline:
            payments = [(req.request_date, safe), (earliest, (requested - safe).quantize(CENT))]
            ok, _ = plan_is_safe(ledger, base_items, payments, cfg, rule=cfg.earliest_rule)
            if ok:
                cands.append(Plan("partial_payment", payments, [], completes_by_deadline=True, total_paid=requested, safe=True))
            else:
                an.rejected.append("partial_payment: re-simulation failed")
        else:
            an.rejected.append("partial_payment: conditions not met (0 < safe < requested and earliest <= deadline)")
    elif "partial_payment" in prof.methods:
        an.rejected.append("partial_payment: request does not allow partial payment")

    # installments: exact supplied schedules only
    for o in eligible_options(req, options, ledger, an.rejected):
        sched = o.schedule()
        ok, low_o = plan_is_safe(ledger, base_items, sched, cfg)
        if ok:
            cands.append(Plan("installments", sched, [], option_id=o.payment_option_id, option_number=o.option_number,
                              completes_by_deadline=sched[-1][0] <= deadline, total_paid=o.total_payable_amount.quantize(CENT),
                              min_balance=low_o, safe=True))
        else:
            an.rejected.append(f"{o.payment_option_id}: unsafe (min balance {low_o} < {ledger.minimum})")

    # spending-change plans: only needed when nothing change-free completes by the deadline
    if not any(c.completes_by_deadline for c in cands):
        combos = enumerate_changes(ledger, cfg)
        for combo in combos:
            items = build_items(ledger, cfg, combo)
            found = False
            if "full_payment" in prof.methods and "full_payment" in cfg.change_methods:
                ok, low_c = plan_is_safe(ledger, items, [(req.request_date, requested)], cfg)
                if ok:
                    cands.append(Plan("full_payment", [(req.request_date, requested)], combo, completes_by_deadline=True,
                                      total_paid=requested, min_balance=low_c, safe=True))
                    found = True
            if not found and "installments" in cfg.change_methods:
                ranked = sorted(eligible_options(req, options, ledger, []),
                                key=lambda o: (o.total_payable_amount, o.first_payment_date, o.number_of_payments, o.option_number,
                                               o.payment_option_id))
                for o in ranked:
                    sched = o.schedule()
                    if sched[-1][0] > deadline:
                        continue
                    ok, low_c = plan_is_safe(ledger, items, sched, cfg)
                    if ok:
                        cands.append(Plan("installments", sched, combo, option_id=o.payment_option_id, option_number=o.option_number,
                                          completes_by_deadline=True, total_paid=o.total_payable_amount.quantize(CENT),
                                          min_balance=low_c, safe=True))
                        found = True
                        break
            if found:
                break  # combos are ordered by total reduction: first feasible = smallest change

    an.candidates = sorted(cands, key=rank_key)
    an.chosen = an.candidates[0] if an.candidates else None
    return an


def status_for(plan: Optional[Plan]) -> tuple[str, str]:
    if plan is None:
        return "not_affordable", "not_recommended"
    if plan.method == "full_payment":
        return ("affordable_with_plan" if plan.changes else "affordable_now"), "full_payment"
    if plan.method == "wait":
        return "affordable_later", "wait"
    if plan.method == "partial_payment":
        return "affordable_with_plan", "partial_payment"
    if plan.method == "installments":
        return "affordable_with_plan", "installments"
    return "not_affordable", "not_recommended"
