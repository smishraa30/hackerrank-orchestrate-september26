"""Concise, grounded explanations rendered from the analysis (no free-form model text).

Every sentence is derived from ledger facts: the minimum balance, the lowest projected balance and its
date, the next confirmed income, reserved one-off debits, the plan chosen and why the alternatives lost.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .config import EngineConfig
from .forecast import balance_path, build_items
from .ledger import Ledger
from .models import Request, SpendingChange
from .planner import Analysis
from .render import fmt_date_long, fmt_money


def _series_desc(ledger: Ledger, ch: SpendingChange) -> str:
    for s in ledger.series:
        if s.key == ch.series_key:
            if s.occurrences:
                return s.occurrences[-1].description.lower()
            return s.category.replace("_", " ")
    return ch.series_key.split("|")[1].replace("_", " ")


def _facts(ledger: Ledger, cfg: EngineConfig) -> dict:
    items = build_items(ledger, cfg)
    path = balance_path(ledger, items, cfg)
    low = min(path.lows)
    low_date = path.days[path.lows.index(low)]
    credits = sorted((c.day, c.amount, c.label) for c in items if c.amount > 0)
    nxt = credits[0] if credits else None
    one_offs = [c for c in ledger.one_offs if c.amount < 0 and ledger.start <= c.day <= ledger.end]
    biggest_one_off = max(one_offs, key=lambda c: -c.amount) if one_offs else None
    return {"low": low, "low_date": low_date, "next_income": nxt, "one_off": biggest_one_off, "n_credits": len(credits)}


def _income_phrase(ledger: Ledger, f: dict, ccy: str) -> str:
    nxt = f["next_income"]
    if nxt is None:
        return "no confirmed income is projected in the forecast window"
    d, a, label = nxt
    what = "salary" if "salary" in label else ("income" if "invoice" not in label else "invoice payment")
    return f"the {fmt_money(a, ccy)} {what} on {fmt_date_long(d)}"


def _changes_sentence(ledger: Ledger, changes: list[SpendingChange], ccy: str) -> str:
    parts = []
    for ch in changes:
        desc = _series_desc(ledger, ch)
        if ch.action == "stop":
            parts.append(f"stop the {desc}")
        else:
            parts.append(f"reduce the {desc} to {fmt_money(ch.new_amount or Decimal(0), ccy)}")
    if not parts:
        return ""
    text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return text[0].upper() + text[1:]


def explain(req: Request, ledger: Ledger, an: Analysis, cfg: Optional[EngineConfig] = None) -> str:
    cfg = cfg or EngineConfig()
    ccy = ledger.profile.home_currency
    mn = fmt_money(ledger.minimum, ccy)
    requested = fmt_money(req.requested_amount, ccy)
    safe = fmt_money(an.amount_safe, ccy)
    plan = an.chosen
    deadline = fmt_date_long(req.desired_completion_date)
    if an.blocked_reason:
        return (f"Do not make this payment by {deadline}. A future debit has no usable amount in the supplied data "
                f"({an.blocked_reason.split(';')[0].strip()}), so the {mn} minimum cannot be shown to stay protected "
                f"and no payment is recommended until the amount is confirmed.")
    f = _facts(ledger, cfg)
    income = _income_phrase(ledger, f, ccy)
    low_txt = f"{fmt_money(f['low'], ccy)} on {fmt_date_long(f['low_date'])}"
    reserve = ""
    if f["one_off"] is not None:
        reserve = f" (including the reserved {fmt_money(-f['one_off'].amount, ccy)} {f['one_off'].label.lower()} on {fmt_date_long(f['one_off'].day)})"

    if plan is None:
        methods = ", ".join(m.replace("_", " ") for m in ledger.profile.methods) or "no payment method"
        if an.earliest is not None:
            return (f"Do not make this payment by {deadline}. Only {safe} is safe today and the full {requested} only becomes safe on "
                    f"{fmt_date_long(an.earliest)}, but you only accept {methods}, and none of those keeps the {mn} minimum protected.")
        if an.amount_safe > 0:
            return (f"Do not proceed with the {requested} request. Although {safe} is available today, the full amount cannot be "
                    f"completed safely within 90 days: the projected balance bottoms out at {low_txt}{reserve} against the {mn} minimum, "
                    f"and {income if f['next_income'] is not None else 'no confirmed income is projected in the window'}.")
        return (f"Do not make this payment by {deadline}. The projected balance already falls to {low_txt}{reserve}, below the {mn} "
                f"minimum, so no payment option is safe.")

    if plan.method == "full_payment" and not plan.changes:
        after = f["low"] - req.requested_amount
        with_income = f", with {income}" if f["next_income"] is not None else ""
        return (f"Pay {requested} today. Even after paying, the balance stays above the {mn} minimum for the next 90 days "
                f"(lowest point {fmt_money(after, ccy)} on {fmt_date_long(f['low_date'])}{reserve}){with_income}.")

    if plan.method == "full_payment" and plan.changes:
        helped = f", helped by {income}" if f["next_income"] is not None else ""
        until = f"until {fmt_date_long(an.earliest)}, past the {deadline} deadline" if an.earliest else "within the forecast window"
        return (f"{_changes_sentence(ledger, plan.changes, ccy)}, then pay {requested} today. Without these changes only {safe} is safe "
                f"today and the full amount is not safe {until}; with them the balance stays above the {mn} minimum{helped}.")

    if plan.method == "wait":
        d = plan.payments[0][0]
        lead = (f"Pay {requested} in full on {fmt_date_long(d)}." if d == req.desired_completion_date
                else f"Wait until {fmt_date_long(d)}, then pay {requested} in full.")
        tail = (f" Only {safe} is safe today: paying earlier would take the balance below the {mn} minimum "
                f"(lowest projected balance {low_txt}{reserve}); after {income} the full payment keeps the minimum protected."
                if f["next_income"] is not None else
                f" Only {safe} is safe today: paying earlier would take the balance below the {mn} minimum "
                f"(lowest projected balance {low_txt}{reserve}).")
        if d > req.desired_completion_date:
            tail += f" This is after the {deadline} deadline, but no earlier plan is safe."
        return lead + tail

    if plan.method == "partial_payment":
        (d1, a1), (d2, a2) = plan.payments
        return (f"Pay {fmt_money(a1, ccy)} today and the remaining {fmt_money(a2, ccy)} on {fmt_date_long(d2)}. "
                f"{fmt_money(a1, ccy)} is the most the balance can absorb before {income}; the second payment completes the request "
                f"by the {deadline} deadline and keeps the {mn} minimum protected.")

    if plan.method == "installments":
        n = plan.n_payments
        amt = fmt_money(plan.payments[0][1], ccy)
        start = fmt_date_long(plan.payments[0][0])
        total = fmt_money(plan.total_paid, ccy)
        prefix = (f"{_changes_sentence(ledger, plan.changes, ccy)}, then use" if plan.changes else "Use")
        why = (f"Paying {requested} today is not safe (only {safe} is available above the {mn} minimum)"
               if an.amount_safe < req.requested_amount else "You do not accept a single full payment")
        covered = f"the installments are covered by {income}" if f["next_income"] is not None else "the spread-out installments fit the existing balance"
        return f"{prefix} {n} installments of {amt}, starting {start} (total {total}). {why}; {covered} and keep at least {mn} available."

    return f"Do not make this payment by {deadline}. None of the available options keeps the {mn} minimum protected."
