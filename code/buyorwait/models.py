"""Typed internal models. All money is Decimal, all dates are datetime.date."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class Profile:
    user_id: str
    home_currency: str
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal
    financial_priorities: tuple[str, ...]
    protect: tuple[str, ...]
    reduce: tuple[str, ...]
    stop: tuple[str, ...]
    methods: tuple[str, ...]
    max_installment_months: Optional[int]


@dataclass
class Event:
    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: str  # debit | credit | non_cash
    amount: Optional[Decimal]  # None when blank in the CSV (image-backed)
    currency: str
    event_date: Optional[date]
    settlement_date: Optional[date]
    status: str  # settled | pending | scheduled | cancelled | failed | unrealized
    linked_event_id: str
    flexibility: str  # fixed | reducible | stoppable | reducible_or_stoppable
    minimum_allowed_amount: Optional[Decimal]
    # derived
    home_amount: Optional[Decimal] = None  # amount converted to home currency
    amount_source: str = "csv"  # csv | image | message
    notes: list[str] = field(default_factory=list)

    @property
    def is_flexible(self) -> bool:
        return self.flexibility in ("reducible", "stoppable", "reducible_or_stoppable")

    @property
    def can_stop(self) -> bool:
        return self.flexibility in ("stoppable", "reducible_or_stoppable")

    @property
    def can_reduce(self) -> bool:
        return self.flexibility in ("reducible", "reducible_or_stoppable") and self.minimum_allowed_amount is not None


@dataclass(frozen=True)
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: Decimal
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str
    malformed: str = ""  # non-empty when the CSV row could not be parsed; the engine must not evaluate it


@dataclass(frozen=True)
class DataIssue:
    """An explicit data-quality problem found while reconstructing a ledger.

    severity "blocking": the forecast cannot be bounded (an unknown future debit); the engine must fall back to
    the conservative decision. severity "warning": recorded for provenance only.
    """

    code: str
    severity: str
    detail: str
    event_id: Optional[str] = None


@dataclass(frozen=True)
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: str  # full_payment | installments
    payment_amount: Decimal
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: Optional[int]
    financing_fee: Decimal
    total_payable_amount: Decimal

    @property
    def option_number(self) -> int:
        """Numeric part of the option id (lowest id is the final ranking tie-breaker); ids without a
        trailing number sort last and are then ordered by their text."""
        import re

        m = re.search(r"(\d+)\s*$", self.payment_option_id)
        return int(m.group(1)) if m else 10**9

    def schedule(self) -> list[tuple[date, Decimal]]:
        from datetime import timedelta

        out = []
        step = self.payment_frequency_days or 0
        for k in range(self.number_of_payments):
            out.append((self.first_payment_date + timedelta(days=step * k), self.payment_amount))
        return out


@dataclass(frozen=True)
class Message:
    message_id: str
    user_id: str
    request_id: str
    related_event_id: str
    sent_at: date
    source_type: str
    message_text: str


@dataclass(frozen=True)
class ImageRef:
    image_id: str
    user_id: str
    request_id: str
    related_event_id: str


@dataclass
class SampleExpectation:
    amount_safe_to_pay: str
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str
    earliest_date_for_full_payment: str
    spending_changes_needed: str
    decision_explanation: str


@dataclass
class CashItem:
    """One dated cash movement inside the forecast window (home currency)."""

    day: date
    amount: Decimal  # positive = credit, negative = debit
    kind: str  # recurring | pending | scheduled | plan | message
    label: str
    series_key: Optional[str] = None
    event_id: Optional[str] = None


@dataclass
class Series:
    """A detected recurring series."""

    key: str
    event_type: str
    category: str
    direction: str
    cadence: str  # monthly | interval
    day_of_month: Optional[int]
    interval_days: Optional[int]
    amount: Decimal  # forecast amount per occurrence (native currency of the series)
    currency: str
    last_date: date
    last_event_id: str
    occurrences: list[Event]
    flexibility: str
    minimum_allowed_amount: Optional[Decimal]
    active: bool = True
    notes: list[str] = field(default_factory=list)
    # message-driven overrides
    amount_from: dict[date, Decimal] = field(default_factory=dict)  # date -> native amount for occurrences on/after date
    date_override: Optional[date] = None  # next occurrence forced to this date (monthly day shifts)
    stop_after: Optional[date] = None  # no occurrences after this date
    start_from: Optional[date] = None  # no occurrences before this date
    first_date: Optional[date] = None  # first historical occurrence (for cadence variants)

    @property
    def is_flexible(self) -> bool:
        return self.flexibility in ("reducible", "stoppable", "reducible_or_stoppable")

    @property
    def can_stop(self) -> bool:
        return self.flexibility in ("stoppable", "reducible_or_stoppable")

    @property
    def can_reduce(self) -> bool:
        return self.flexibility in ("reducible", "reducible_or_stoppable") and self.minimum_allowed_amount is not None


@dataclass
class SpendingChange:
    action: str  # stop | reduce_to
    event_id: str
    series_key: str
    new_amount: Optional[Decimal] = None

    def render(self) -> str:
        from .render import fmt_plan_amount

        if self.action == "stop":
            return f"stop:{self.event_id}"
        return f"reduce_to:{self.event_id}:{fmt_plan_amount(self.new_amount)}"


@dataclass
class Plan:
    method: str  # full_payment | partial_payment | installments | wait | not_recommended
    payments: list[tuple[date, Decimal]]
    changes: list[SpendingChange]
    option_id: Optional[str] = None
    option_number: int = 10**9
    completes_by_deadline: bool = True
    total_paid: Decimal = Decimal(0)
    min_balance: Optional[Decimal] = None
    safe: bool = False

    @property
    def start_date(self) -> Optional[date]:
        return self.payments[0][0] if self.payments else None

    @property
    def n_payments(self) -> int:
        return len(self.payments)


@dataclass
class Decision:
    request_id: str
    amount_safe_to_pay: Decimal
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str
    earliest_date_for_full_payment: Optional[date]
    spending_changes_needed: str
    decision_explanation: str
    trace: dict = field(default_factory=dict)
