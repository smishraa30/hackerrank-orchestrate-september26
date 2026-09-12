"""Ledger reconstruction: normalize events, resolve evidence, detect recurrence.

Output: a `Ledger` holding the opening balance, the minimum balance, the active
recurring series (native currency, converted per occurrence date by the
forecaster) and the dated one-off cash items inside the forecast window.
Every derived fact carries a note for provenance.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from .config import EngineConfig
from .fx import CENT, RateTable
from .images import ImageExtractor
from .ingest import Dataset
from .messages import Amendment, interpret_all
from .models import CashItem, DataIssue, Event, Profile, Request, Series

NON_RECURRING_TYPES = {"refund", "investment_purchase", "investment_valuation", "investment_sale"}
NON_RECURRING_CATEGORIES = {"windfall", "work_expense", "investment"}


@dataclass
class Ledger:
    profile: Profile
    request: Request
    start: date
    end: date
    balance: Decimal
    minimum: Decimal
    series: list[Series]
    one_offs: list[CashItem]
    explicit_by_key: dict[str, list[date]]  # settlement and event dates of explicit rows per series key
    amendments: list[Amendment]
    rates: RateTable
    notes: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    issues: list[DataIssue] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """True when a future debit has an unknown amount: no forecast can be bounded, so the engine must
        answer conservatively instead of silently ignoring the debit."""
        return any(i.severity == "blocking" for i in self.issues)

    def flexible_series(self) -> list[Series]:
        """Series the user permits to stop or reduce (never protected categories)."""
        p = self.profile
        out = []
        for s in self.series:
            if not s.active or s.direction != "debit" or not s.is_flexible:
                continue
            if s.category in p.protect:
                continue
            if (s.can_stop and s.category in p.stop) or (s.can_reduce and s.category in p.reduce):
                out.append(s)
        return out


def _estimate(amounts: list[Decimal], estimator: str, cfg: EngineConfig) -> Decimal:
    if not amounts:
        return Decimal(0)
    if len(set(amounts)) == 1:
        return amounts[0]
    med = statistics.median(amounts)
    kept = [a for a in amounts if med == 0 or (Decimal(str(cfg.outlier_lo)) * med <= a <= Decimal(str(cfg.outlier_hi)) * med)]
    if not kept:
        kept = amounts
    if estimator == "median":
        v = statistics.median(kept)
    elif estimator == "last":
        v = kept[-1]
    elif estimator == "max":
        v = max(kept)
    elif estimator == "mean3":
        v = statistics.mean(kept[-3:])
    elif estimator == "midrange":
        v = (max(kept) + min(kept)) / 2
    elif estimator == "trimmed_mean":
        s = sorted(kept)
        k = len(s) // 5
        s = s[k: len(s) - k] if len(s) - 2 * k >= 1 else s
        v = statistics.mean(s)
    else:
        v = statistics.mean(kept)
    return Decimal(v).quantize(CENT)


def monthly_projection_end(R: date, cfg: EngineConfig) -> date:
    """Last day on which monthly templates are projected."""
    if cfg.horizon_mode in ("calendar", "hybrid"):
        import calendar as _cal
        y, m = R.year, R.month + 2
        if m > 12:
            y, m = y + 1, m - 12
        return min(date(y, m, _cal.monthrange(y, m)[1]), horizon_end(R, cfg))
    return horizon_end(R, cfg)


def horizon_end(R: date, cfg: EngineConfig) -> date:
    if cfg.horizon_mode == "calendar":
        import calendar as _cal
        y, m = R.year, R.month + 2
        if m > 12:
            y, m = y + 1, m - 12
        return date(y, m, _cal.monthrange(y, m)[1])
    return R + timedelta(days=cfg.horizon_days)


def _eday(e: Event) -> date:
    return e.event_date or e.settlement_date or date.min


def _sday(e: Event) -> date:
    return e.settlement_date or e.event_date or date.min


class LedgerBuilder:
    def __init__(self, ds: Dataset, extractor: ImageExtractor, cfg: Optional[EngineConfig] = None):
        self.ds = ds
        self.extractor = extractor
        self.cfg = cfg or EngineConfig()
        self.rates = RateTable(ds.rates)

    # ------------------------------------------------------------------
    def build(self, req: Request) -> Ledger:
        cfg = self.cfg
        prof = self.ds.profiles[req.user_id]
        R = req.request_date
        end = horizon_end(R, cfg)
        notes: list[str] = []
        prov: dict = {"images": [], "fx": [], "messages": []}
        home = prof.home_currency

        events = [self._copy(e) for e in self.ds.events_by_user.get(req.user_id, [])]
        issues: list[DataIssue] = []

        def unknown_amount(e: Event, why: str) -> None:
            """A row whose cash amount cannot be established. Unknown *future debits* block the forecast
            (conservative); everything else is only recorded (ignoring unknown credits is the safe direction)."""
            future = e.status in ("pending", "scheduled") or (_sday(e) >= R)
            if e.direction == "debit" and future and e.status not in ("cancelled", "failed", "unrealized"):
                issues.append(DataIssue("unknown_debit_amount", "blocking", f"{e.event_id}: {why}", e.event_id))
                notes.append(f"{e.event_id}: {why}; unknown FUTURE DEBIT -> forecast cannot be bounded (blocking)")
            elif e.direction == "debit":
                issues.append(DataIssue("unknown_history_amount", "warning", f"{e.event_id}: {why}", e.event_id))
                notes.append(f"{e.event_id}: {why}; historical row excluded from recurrence statistics")
            else:
                issues.append(DataIssue("unknown_credit_amount", "warning", f"{e.event_id}: {why}", e.event_id))
                notes.append(f"{e.event_id}: {why}; credit ignored (never invented)")

        # 1. amounts: images for blanks, then currency conversion at settlement date
        for e in events:
            if e.amount is None and e.notes:
                unknown_amount(e, "; ".join(e.notes))
                continue
            if e.amount is None:
                img = self.ds.images_by_event.get(e.event_id)
                if img is not None:
                    res = self.extractor.extract(
                        self.ds.image_path(img.image_id), img.image_id,
                        {"event_id": e.event_id, "description": e.description, "category": e.category,
                         "currency": e.currency, "event_date": str(e.event_date), "status": e.status},
                    )
                    if res is not None:
                        e.amount = Decimal(res["amount"]).quantize(CENT)
                        e.amount_source = "image:" + img.image_id
                        prov["images"].append({"event_id": e.event_id, "image_id": img.image_id, "amount": str(e.amount),
                                               "currency": e.currency, "confidence": res.get("confidence"),
                                               "method": res.get("method"), "field_used": res.get("field_used")})
                        notes.append(f"{e.event_id}: blank amount resolved from {img.image_id} = {e.currency} {e.amount} ({res.get('field_used')})")
                    else:
                        unknown_amount(e, f"blank amount and no usable extraction from {img.image_id} (never zero)")
                        continue
                else:
                    unknown_amount(e, "blank amount without a linked image (never zero)")
                    continue
            if e.amount is not None:
                if e.currency != home:
                    try:
                        e.home_amount, fxp = self.rates.convert(e.amount, _sday(e), e.currency, home)
                    except KeyError:
                        unknown_amount(e, f"no supplied exchange rate for {e.currency}->{home} (never guessed)")
                        continue
                    prov["fx"].append({"event_id": e.event_id, "from": e.currency, "to": home, "date": str(_sday(e)),
                                       "rate": fxp, "home_amount": str(e.home_amount)})
                else:
                    e.home_amount = e.amount.quantize(CENT)

        # 2. messages -> amendments (structured facts only); only messages sent on/before the request date
        all_msgs = self.ds.messages_by_user.get(req.user_id, [])
        usable_msgs = [m for m in all_msgs if m.sent_at <= R]
        for m in all_msgs:
            if m.sent_at > R:
                issues.append(DataIssue("future_message_ignored", "warning",
                                        f"{m.message_id} sent {m.sent_at} after request date", None))
                notes.append(f"{m.message_id}: sent {m.sent_at}, after the request date {R}; ignored (no look-ahead)")
        amendments = interpret_all(usable_msgs)
        for a in amendments:
            prov["messages"].append({"message_id": a.message_id, "rule": a.rule, "kind": a.kind,
                                     "amount": str(a.amount) if a.amount is not None else None, "currency": a.currency,
                                     "date": str(a.day) if a.day else None, "note": a.note})
        keep_pending = {a.event_id for a in amendments if a.kind == "keep_pending_debit" and a.event_id}
        retry_failed = {a.event_id for a in amendments if a.kind == "retry_failed" and a.event_id}
        transfer_ids = self._own_transfers(events) if any(a.kind == "own_transfer" for a in amendments) else set()

        # 3. classify rows by cash state
        history: list[Event] = []
        one_offs: list[CashItem] = []
        explicit_by_key: dict[str, set[date]] = defaultdict(set)
        scheduled_income: list[Event] = []
        for e in events:
            if e.home_amount is None or e.direction == "non_cash":
                continue
            key = self._key(e)
            sd = _sday(e)
            if e.status == "settled":
                if sd < R:
                    if e.event_id in transfer_ids:
                        notes.append(f"{e.event_id}: internal transfer, excluded from recurrence")
                        continue
                    history.append(e)
                elif sd <= end:
                    if e.direction == "credit":
                        notes.append(f"{e.event_id}: settled credit dated after the request date; not counted until it is in the balance")
                        continue
                    one_offs.append(CashItem(sd, self._signed(e), "settled_future", e.description, key, e.event_id))
                    explicit_by_key[key].update({sd, _eday(e)})
                continue
            if e.status in ("unrealized", "cancelled"):
                continue
            if e.status == "failed":
                if e.event_id in retry_failed and not self._has_retry(events, e):
                    day = max(R, sd)
                    one_offs.append(CashItem(day, self._signed(e), "retry", f"retry of failed {e.description}", key, e.event_id))
                    notes.append(f"{e.event_id}: failed debit will be retried per bank message; reserved on {day}")
                continue
            if e.status == "pending":
                if e.direction == "credit":
                    notes.append(f"{e.event_id}: pending credit ignored until it settles")
                    continue
                if e.description.lower().startswith("possible duplicate") and not (e.event_id in keep_pending or cfg.duplicate_default_keep):
                    notes.append(f"{e.event_id}: possible duplicate charge ignored")
                    continue
                if not cfg.include_pending_debits:
                    continue
                day = max(R, sd)
                if day <= end:
                    one_offs.append(CashItem(day, self._signed(e), "pending", e.description, key, e.event_id))
                continue
            if e.status == "scheduled":
                day = max(R, sd)
                if e.direction == "credit":
                    if not (e.event_type == "income" and e.category == "salary"):
                        notes.append(f"{e.event_id}: scheduled non-salary credit ignored until it settles")
                        continue
                    scheduled_income.append(e)
                if day <= end:
                    one_offs.append(CashItem(day, self._signed(e), "scheduled", e.description, key, e.event_id))
                    explicit_by_key[key].update({day, _eday(e)})
                continue

        # 4. recurrence detection on settled history
        series = self._detect_series(history, R, prof)

        # 5. scheduled income rows confirm/define the salary series
        for e in scheduled_income:
            key = self._key(e)
            sd = _sday(e)
            s = next((x for x in series if x.key == key), None)
            if s is None:
                s = Series(key=key, event_type=e.event_type, category=e.category, direction="credit", cadence="monthly",
                           day_of_month=sd.day, interval_days=None, amount=e.amount, currency=e.currency, last_date=sd,
                           last_event_id=e.event_id, occurrences=[], flexibility="fixed", minimum_allowed_amount=None)
                s.notes.append(f"series created from scheduled {e.event_id}")
                series.append(s)
            else:
                s.active = True
                s.amount = e.amount if e.currency == s.currency else s.amount
                s.day_of_month = sd.day
                s.notes.append(f"confirmed by scheduled {e.event_id}: {e.currency} {e.amount} on {sd}")

        # 6. message amendments
        self._apply_amendments(series, one_offs, amendments, prof, R, end, notes)

        return Ledger(profile=prof, request=req, start=R, end=end, balance=prof.current_available_balance,
                      minimum=prof.minimum_balance_to_keep, series=series,
                      one_offs=sorted(one_offs, key=lambda c: (c.day, c.event_id or "")),
                      explicit_by_key={k: sorted(v) for k, v in explicit_by_key.items()}, amendments=amendments,
                      rates=self.rates, notes=notes, provenance=prov, issues=issues)

    # ------------------------------------------------------------------
    @staticmethod
    def _copy(e: Event) -> Event:
        return Event(**{k: (list(v) if isinstance(v, list) else v) for k, v in e.__dict__.items()})

    @staticmethod
    def _key(e: Event) -> str:
        return f"{e.event_type}|{e.category}|{e.direction}"

    @staticmethod
    def _signed(e: Event) -> Decimal:
        return e.home_amount if e.direction == "credit" else -e.home_amount

    @staticmethod
    def _has_retry(events: list[Event], failed: Event) -> bool:
        return any(x.linked_event_id == failed.event_id and x.status == "scheduled" for x in events)

    @staticmethod
    def _own_transfers(events: list[Event]) -> set[str]:
        out: set[str] = set()
        settled = [e for e in events if e.status == "settled" and e.home_amount is not None]
        for a in settled:
            if a.direction != "debit" or a.event_type in NON_RECURRING_TYPES:
                continue
            for b in settled:
                if (b.direction == "credit" and b.home_amount == a.home_amount and b.event_type not in NON_RECURRING_TYPES
                        and b.category != "salary" and abs((_sday(a) - _sday(b)).days) <= 3):
                    out.add(a.event_id)
                    out.add(b.event_id)
        return out

    # ------------------------------------------------------------------
    def _detect_series(self, history: list[Event], R: date, prof: Profile) -> list[Series]:
        cfg = self.cfg
        groups: dict[str, list[Event]] = defaultdict(list)
        for e in history:
            if e.event_type in NON_RECURRING_TYPES or e.category in NON_RECURRING_CATEGORIES:
                continue
            if e.linked_event_id and e.event_type == "expense":
                continue  # settled purchase replacing a cancelled authorisation: one-off
            groups[self._key(e)].append(e)
        series: list[Series] = []
        for key, evs in sorted(groups.items()):
            evs.sort(key=lambda e: (_eday(e), e.event_id))
            remaining = list(evs)
            for _ in range(3):  # up to three sub-series per key (e.g. two incomes)
                if len(remaining) < cfg.min_occurrences:
                    break
                found = self._extract_monthly(remaining, key, R, prof) or self._extract_interval(remaining, key, R, prof)
                if not found:
                    break
                s, used = found
                series.append(s)
                used_ids = {e.event_id for e in used}
                remaining = [e for e in remaining if e.event_id not in used_ids]
        return series

    def _make_series(self, members: list[Event], key: str, cadence: str, dom: Optional[int], interval: Optional[int],
                     R: date, prof: Profile) -> Series:
        cfg = self.cfg
        et, cat, direction = key.split("|")
        ccy = members[-1].currency
        amounts = [e.amount for e in members if e.currency == ccy]
        last = members[-1]
        notes: list[str] = []
        # "fixed" = identical amounts (the whole history, or a new level held for the last three occurrences);
        # two equal amounts in a row are not enough to call a variable series fixed
        fixed = len(set(amounts)) == 1 or (len(amounts) >= 3 and len(set(amounts[-3:])) == 1)
        if direction == "credit" and not fixed and len(amounts) >= 2 and amounts[-1] == amounts[-2]:
            fixed = True  # salary level changes are step functions; two equal recent payrolls confirm the new level
        if direction == "credit":
            if fixed:
                amount = amounts[-1]
            else:
                mode, n = Counter(amounts).most_common(1)[0]
                if n * 2 >= len(amounts) and n >= 2:
                    amount, fixed = mode, True
                    notes.append(f"income amount taken as mode {mode} ({n}/{len(amounts)})")
                else:
                    amount = _estimate(amounts, cfg.estimator, cfg)
        else:
            amount = amounts[-1] if fixed else _estimate(amounts, cfg.estimator, cfg)
        last_day = _eday(last)
        if cadence == "monthly":
            active = (R - last_day).days <= cfg.monthly_inactive_days
        else:
            active = (R - last_day).days <= cfg.interval_inactive_factor * (interval or 30) + 1
        if direction == "credit" and cfg.income_requires_fixed_amount and not fixed:
            active = False
            notes.append("variable-amount income not projected (unconfirmed)")
        if direction == "credit" and any(w in last.description.lower() for w in ("final ", "final-", "last payroll", "final settlement")):
            active = False
            notes.append(f"income series ended: last row described as '{last.description}'")
        s = Series(key=key, event_type=et, category=cat, direction=direction, cadence=cadence, day_of_month=dom,
                   interval_days=interval, amount=amount, currency=ccy, last_date=last_day, last_event_id=last.event_id,
                   occurrences=members, flexibility=last.flexibility, minimum_allowed_amount=last.minimum_allowed_amount,
                   active=active, notes=notes)
        s.first_date = _eday(members[0])
        s.notes.append(f"{len(members)} occurrences, cadence={cadence}, dom={dom}, interval={interval}, "
                       f"amount={ccy} {amount}, fixed={fixed}, active={active}, last={last_day}")
        return s

    def _extract_monthly(self, evs: list[Event], key: str, R: date, prof: Profile):
        cfg = self.cfg
        if len(evs) < cfg.min_occurrences:
            return None
        import calendar as _cal

        def month_end(e: Event) -> bool:
            d = _eday(e)
            return d.day >= _cal.monthrange(d.year, d.month)[1] - 2

        doms = Counter(_eday(e).day for e in evs)
        dom, _ = doms.most_common(1)[0]
        if dom >= 26 and sum(1 for e in evs if month_end(e)) * 2 >= len(evs):
            dom = 31  # month-end bill: project on the last day of each month (clamped per month)
            members = [e for e in evs if month_end(e)]
        else:
            members = [e for e in evs if abs(_eday(e).day - dom) <= 1]
        # one occurrence per calendar month: keep the row closest to the dominant day, others are one-offs
        by_month: dict[tuple[int, int], Event] = {}
        med = statistics.median([e.amount for e in evs if e.amount is not None]) if evs else Decimal(0)

        def rank(e: Event):  # closest to the dominant day, then closest to the group's typical amount
            return (abs(_eday(e).day - dom), abs((e.amount or Decimal(0)) - med), e.event_id)

        for e in members:
            ym = (_eday(e).year, _eday(e).month)
            cur = by_month.get(ym)
            if cur is None or rank(e) < rank(cur):
                by_month[ym] = e
        members = sorted(by_month.values(), key=lambda e: (_eday(e), e.event_id))
        if len(members) < cfg.min_occurrences:
            return None
        months = sorted(by_month)
        span = (months[-1][0] - months[0][0]) * 12 + (months[-1][1] - months[0][1]) + 1
        if len(members) / span < 0.6:
            return None
        if len(members) / len(evs) < 0.5 and len(evs) - len(members) >= 2:
            gaps = [(_eday(evs[i + 1]) - _eday(evs[i])).days for i in range(len(evs) - 1)]
            if gaps and statistics.median(gaps) < 25:
                return None  # interval series that merely shares a day-of-month
        return self._make_series(members, key, "monthly", dom, None, R, prof), members

    def _extract_interval(self, evs: list[Event], key: str, R: date, prof: Profile):
        cfg = self.cfg
        if len(evs) < cfg.min_occurrences:
            return None
        days = [_eday(e) for e in evs]
        gaps = [(days[i + 1] - days[i]).days for i in range(len(days) - 1)]
        if not gaps:
            return None
        g = int(round(statistics.median(gaps)))
        if g < 2 or g > 35:
            return None
        if sum(abs(x - g) <= 2 for x in gaps) / len(gaps) < 0.6:
            return None
        members = [evs[0]]
        for e in evs[1:]:
            gap = (_eday(e) - _eday(members[-1])).days
            if gap < g - 2:
                continue  # extra event inside the cadence -> one-off
            members.append(e)
        if len(members) < cfg.min_occurrences:
            return None
        return self._make_series(members, key, "interval", None, g, R, prof), members

    # ------------------------------------------------------------------
    def _apply_amendments(self, series: list[Series], one_offs: list[CashItem], amendments: list[Amendment],
                          prof: Profile, R: date, end: date, notes: list[str]) -> None:
        home = prof.home_currency
        salary = [s for s in series if s.direction == "credit" and s.category == "salary"]
        primary: Optional[Series] = None
        if salary:
            primary = sorted(salary, key=lambda s: (-len(s.occurrences), s.last_date))[0]
            for s in salary:
                if s.cadence == "monthly" and s.day_of_month == 15 and len(s.occurrences) >= 2:
                    primary = s
                    break

        def in_series_ccy(amount: Decimal, ccy: str, s: Series, day: date) -> Decimal:
            if ccy == s.currency:
                return amount
            try:
                conv, _ = self.rates.convert(amount, day, ccy, s.currency)
            except KeyError:
                s.notes.append(f"message amount {ccy} {amount} could not be converted (no rate); history amount kept")
                return s.amount
            s.notes.append(f"message amount {ccy} {amount} converted to {s.currency} {conv} at {day}")
            return conv

        for a in amendments:
            if a.kind == "salary_amount" and a.amount is not None:
                ccy = a.currency or home
                if primary is None:
                    day = a.day or R
                    primary = Series(key="income|salary|credit", event_type="income", category="salary", direction="credit",
                                     cadence="monthly", day_of_month=day.day, interval_days=None, amount=a.amount, currency=ccy,
                                     last_date=day - timedelta(days=31), last_event_id="", occurrences=[], flexibility="fixed",
                                     minimum_allowed_amount=None, active=True, start_from=day)
                    primary.notes.append(f"{a.message_id}: salary series created from message ({a.rule}) {ccy} {a.amount} on {day}")
                    series.append(primary)
                else:
                    primary.active = True
                    day = a.day or R
                    primary.amount_from[day] = in_series_ccy(a.amount, ccy, primary, day)
                    if a.day is not None and a.rule in ("first_salary", "salary_resumes", "foreign_salary_confirmed", "salary_increase"):
                        if a.rule != "salary_increase":
                            primary.date_override = a.day
                            primary.day_of_month = a.day.day
                    primary.notes.append(f"{a.message_id}: amount {ccy} {a.amount} from {day} ({a.rule})")
                notes.append(f"{a.message_id}: {a.note}")
            elif a.kind == "salary_date" and a.day is not None:
                if primary is not None:
                    primary.date_override = a.day
                    primary.day_of_month = a.day.day
                    primary.active = True
                    primary.notes.append(f"{a.message_id}: next payroll on {a.day}; day-of-month {a.day.day} thereafter")
                notes.append(f"{a.message_id}: {a.note}")
            elif a.kind == "income_stop":
                for s in series:
                    if s.direction == "credit":
                        s.active = False
                        s.notes.append(f"{a.message_id}: income ended")
                notes.append(f"{a.message_id}: {a.note}")
            elif a.kind in ("exclude_variable_income", "exclude_secondary_income"):
                for s in series:
                    if s.direction == "credit" and s is not primary:
                        s.active = False
                        s.notes.append(f"{a.message_id}: secondary/variable income excluded")
                notes.append(f"{a.message_id}: {a.note}")
            elif a.kind == "one_off_income" and a.amount is not None and a.day is not None:
                if R <= a.day <= end:
                    try:
                        amt_home, _ = self.rates.convert(a.amount, a.day, a.currency or home, home)
                    except KeyError:
                        notes.append(f"{a.message_id}: approved invoice in {a.currency} has no supplied rate; not counted")
                        amt_home = None
                    if amt_home is not None:
                        one_offs.append(CashItem(a.day, amt_home, "message", f"approved invoice ({a.message_id})",
                                                 "income|invoice|credit", None))
                notes.append(f"{a.message_id}: {a.note}")
            elif a.kind == "rent_increase" and a.factor is not None:
                for s in series:
                    if s.direction == "debit" and s.category == "rent":
                        s.amount = (s.amount * a.factor).quantize(CENT)
                        s.notes.append(f"{a.message_id}: rent x{a.factor} from next payment -> {s.amount}")
                notes.append(f"{a.message_id}: {a.note}")
            else:
                notes.append(f"{a.message_id}: {a.note}")
