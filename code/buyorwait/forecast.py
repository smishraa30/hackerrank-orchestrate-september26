"""Daily 90-day cash-flow forecast and safety checks.

The forecast is a list of dated cash items (recurring projections, one-offs,
plan payments). Balances are evaluated at end of day; with
`intraday="debits_first"` the intraday low (debits applied before credits on
the same day) is checked as well.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from .config import EngineConfig
from .fx import CENT
from .ledger import Ledger
from .models import CashItem, Series, SpendingChange


def _clamp_day(y: int, m: int, day: int) -> date:
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


def series_occurrences(s: Series, start: date, end: date, cfg: EngineConfig) -> list[date]:
    """Projected occurrence dates of a series inside [start, end]."""
    if not s.active:
        return []
    out: list[date] = []
    first_ok = start if cfg.include_request_date else start + timedelta(days=1)
    lower = max(first_ok, (s.start_from or first_ok))
    if s.cadence == "monthly":
        from .ledger import monthly_projection_end

        end = min(end, monthly_projection_end(start, cfg))
        day = s.day_of_month or s.last_date.day
        if s.date_override is not None and s.date_override >= lower:
            d = s.date_override
            if d <= end:
                out.append(d)
            y, m = d.year, d.month
            while True:
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
                nd = _clamp_day(y, m, s.date_override.day)
                if nd > end:
                    break
                out.append(nd)
        else:
            y, m = s.last_date.year, s.last_date.month
            for _ in range(0, 6):
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
                nd = _clamp_day(y, m, day)
                if nd < lower:
                    continue
                if nd > end:
                    break
                out.append(nd)
    else:
        step = s.interval_days or 30
        if step < cfg.min_interval_days:
            return []
        step = cfg.interval_remap.get(step, step)
        stepf = float(step)
        n = len(s.occurrences)
        if s.first_date is not None and n >= 2 and cfg.interval_cadence in ("span_n", "span_n1"):
            span = (s.last_date - s.first_date).days
            div = n if cfg.interval_cadence == "span_n" else (n - 1)
            if div > 0 and span > 0:
                stepf = span / div
        if cfg.interval_anchor == "request":
            base = start + timedelta(days=step)
            k0 = 0
        elif cfg.interval_anchor == "request0":
            base = start
            k0 = 0
        elif cfg.interval_anchor == "shift" and s.first_date is not None:
            # the forecast period repeats the observed history pattern: phase = first occurrence + shift
            # (history window = [R - shift, R); the generator restarts the same phase offset at R)
            shifted = s.first_date + timedelta(days=cfg.history_shift_days)
            while shifted - start > timedelta(days=2 * step):
                shifted -= timedelta(days=step)  # unusually long history: reduce the offset modulo the cadence
            while shifted < start:
                shifted += timedelta(days=step)
            base = shifted
            k0 = 0
        else:
            base = s.last_date
            k0 = 1
        import math
        k = k0
        while True:
            off = stepf * k
            if cfg.interval_rounding == "floor":
                od = math.floor(off + 1e-9)
            elif cfg.interval_rounding == "ceil":
                od = math.ceil(off - 1e-9)
            else:
                od = int(math.floor(off + 0.5))
            d = base + timedelta(days=od)
            if d > end:
                break
            if d >= lower:
                out.append(d)
            k += 1
            if k > 400:
                break
    if s.stop_after is not None:
        out = [d for d in out if d <= s.stop_after]
    return out


def series_amount_on(s: Series, day: date, ledger: Ledger) -> Decimal:
    """Native amount for an occurrence on `day`, converted to home currency."""
    amt = s.amount
    if s.amount_from:
        for d in sorted(s.amount_from):
            if day >= d:
                amt = s.amount_from[d]
    if s.currency != ledger.profile.home_currency:
        try:
            conv, _ = ledger.rates.convert(amt, day, s.currency, ledger.profile.home_currency)
        except KeyError:
            if s.direction == "credit":
                return Decimal(0)  # unconvertible income is never invented
            raise
        return conv
    return amt.quantize(CENT)


def build_items(ledger: Ledger, cfg: EngineConfig, changes: Optional[Iterable[SpendingChange]] = None) -> list[CashItem]:
    """All forecast cash items in the window (no plan payments)."""
    items: list[CashItem] = [c for c in ledger.one_offs if ledger.start <= c.day <= ledger.end]
    change_by_key: dict[str, SpendingChange] = {c.series_key: c for c in (changes or [])}
    for s in ledger.series:
        if not s.active:
            continue
        explicit = ledger.explicit_by_key.get(s.key, [])
        for d in series_occurrences(s, ledger.start, ledger.end, cfg):
            if any(abs((d - x).days) <= cfg.explicit_match_days for x in explicit):
                continue  # explicit scheduled row replaces the projection
            amt = series_amount_on(s, d, ledger)
            ch = change_by_key.get(s.key)
            if ch is not None:
                if ch.action == "stop":
                    continue
                if ch.action == "reduce_to" and ch.new_amount is not None:
                    amt = min(amt, ch.new_amount)
            signed = amt if s.direction == "credit" else -amt
            items.append(CashItem(d, signed, "recurring", f"{s.category} ({s.cadence})", s.key, s.last_event_id))
    items.sort(key=lambda c: (c.day, c.kind != "plan", c.label))
    return items


@dataclass
class Path:
    days: list[date]
    balances: list[Decimal]  # end-of-day balance for each day in `days` (sorted)
    lows: list[Decimal]  # intraday low for each day

    def min_from(self, day: date) -> Decimal:
        vals = [lo for d, lo in zip(self.days, self.lows, strict=True) if d >= day]
        return min(vals) if vals else Decimal("Infinity")


def balance_path(ledger: Ledger, items: list[CashItem], cfg: EngineConfig, extra: Optional[list[tuple[date, Decimal]]] = None) -> Path:
    """Balance for every day in the window.

    Intraday order with cfg.intraday == "debits_first": forecast debits, then credits, then plan payments
    (a payment made on payday is funded by that day's income). The day's low is the minimum running
    balance under that order; with "eod" only the end-of-day balance is checked.
    """
    per_day: dict[date, list[tuple[int, Decimal]]] = {}
    for c in items:
        if c.amount >= 0:
            order = 1
        elif cfg.intraday == "interval_first" and c.label.endswith("(monthly)"):
            order = 2  # monthly debits sharing payday are funded by that day's income
        else:
            order = 0
        per_day.setdefault(c.day, []).append((order, c.amount))
    for d, a in (extra or []):
        per_day.setdefault(d, []).append((2, -a))
    days: list[date] = []
    balances: list[Decimal] = []
    lows: list[Decimal] = []
    bal = ledger.balance
    d = ledger.start
    while d <= ledger.end:
        moves = per_day.get(d, [])
        low = None
        if cfg.intraday in ("debits_first", "interval_first"):
            running = bal
            low = bal
            for _, m in sorted(moves, key=lambda t: t[0]):
                running += m
                low = min(low, running)
        bal = bal + sum((m for _, m in moves), Decimal(0))
        low = bal if low is None else min(low, bal)
        days.append(d)
        balances.append(bal)
        lows.append(low)
        d += timedelta(days=1)
    return Path(days, balances, lows)


def income_days(items: list[CashItem]) -> list[date]:
    return sorted({c.day for c in items if c.amount > 0})


def check_end_for(day: date, incomes: list[date], end: date, rule: str) -> date:
    """Last day that must stay above the minimum for a payment made on `day`."""
    if rule == "window":
        return end
    later = [d for d in incomes if d > day]
    if not later:
        return day
    return min(end, later[0] - timedelta(days=1))


def plan_is_safe(ledger: Ledger, items: list[CashItem], payments: list[tuple[date, Decimal]], cfg: EngineConfig,
                 rule: Optional[str] = None) -> tuple[bool, Decimal]:
    """Re-simulate the plan; safe iff every checked day stays >= minimum and all payments fall inside the window.

    With rule="window" every day of the window is checked. With rule="pay_period" each payment is checked
    from its date until the day before the next projected income (the baseline must also be safe before
    the first payment).
    """
    rule = rule or cfg.plan_rule
    for d, _ in payments:
        if d < ledger.start or d > ledger.end:
            return False, Decimal("-Infinity")
    path = balance_path(ledger, items, cfg, extra=payments)
    if rule == "window" or not payments:
        low = min(path.lows) if path.lows else ledger.balance
        return low >= ledger.minimum, low
    incomes = income_days(items)
    first = min(d for d, _ in payments)
    idx = {d: i for i, d in enumerate(path.days)}
    worst = Decimal("Infinity")
    # baseline before the first payment
    for i in range(0, idx[first]):
        worst = min(worst, path.lows[i])
    for d, _ in payments:
        ce = check_end_for(d, incomes, ledger.end, "pay_period")
        for i in range(idx[d], idx[ce] + 1):
            worst = min(worst, path.lows[i])
    return worst >= ledger.minimum, worst


def amount_safe_today(ledger: Ledger, path: Path, requested: Decimal) -> Decimal:
    low = min(path.lows) if path.lows else ledger.balance
    safe = low - ledger.minimum
    if safe < 0:
        safe = Decimal(0)
    if safe > requested:
        safe = requested
    return safe.quantize(CENT)


def earliest_full_payment_date(ledger: Ledger, path: Path, requested: Decimal, cfg: Optional[EngineConfig] = None,
                               items: Optional[list[CashItem]] = None) -> Optional[date]:
    """First day D such that paying `requested` on D keeps every checked day >= minimum.

    The payment on D is funded after that day's income (end-of-day balance); later days use their
    intraday lows. Checked days: the whole remaining window (rule "window") or the pay period starting
    at D (rule "pay_period"). Days before D (and D's own pre-payment low) must be safe on the baseline.
    """
    rule = (cfg.earliest_rule if cfg else "window")
    n = len(path.days)
    suffix_min = [Decimal(0)] * (n + 1)
    suffix_min[n] = Decimal("Infinity")
    for i in range(n - 1, -1, -1):
        suffix_min[i] = min(suffix_min[i + 1], path.lows[i])
    incomes = income_days(items or [])
    idx = {d: i for i, d in enumerate(path.days)}
    prefix_ok = True
    for i in range(n):
        d = path.days[i]
        if path.lows[i] < ledger.minimum:
            prefix_ok = False
        if not prefix_ok:
            continue
        if rule == "window":
            later = suffix_min[i + 1]
        else:
            ce = check_end_for(d, incomes, ledger.end, rule)
            later = min(path.lows[i + 1: idx[ce] + 1]) if idx[ce] > i else Decimal("Infinity")
        worst = min(path.balances[i], later)
        if worst - requested >= ledger.minimum:
            return d
    return None
