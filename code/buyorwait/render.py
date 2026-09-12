"""Exact output formatting and CSV writing."""
from __future__ import annotations

import csv
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

from .ingest import OUTPUT_COLUMNS
from .models import Decision

CENT = Decimal("0.01")


def fmt_amount(x: Decimal) -> str:
    """Minimal decimal representation with at most 2 decimals (603.3, 737, 17229139.2)."""
    q = Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)
    s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def fmt_plan_amount(x: Decimal) -> str:
    """Two decimals unless integral (620.40, 23.50, 25256, 15952906.67)."""
    q = Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)
    if q == q.to_integral_value():
        return str(int(q))
    return format(q, "f")


def fmt_money(x: Decimal, currency: str) -> str:
    """Human style used in explanations: 'EUR 1,234.50', 'INR 122,400'."""
    q = Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)
    if q == q.to_integral_value():
        return f"{currency} {int(q):,}"
    return f"{currency} {q:,.2f}"


def fmt_date_long(d: date) -> str:
    return f"{d.day} {d.strftime('%B %Y')}"


def render_plan(payments: Iterable[tuple[date, Decimal]]) -> str:
    parts = [f"{d.isoformat()}:{fmt_plan_amount(a)}" for d, a in payments]
    return "|".join(parts) if parts else "none"


def decision_row(dec: Decision) -> dict:
    return {
        "request_id": dec.request_id,
        "amount_safe_to_pay": fmt_amount(dec.amount_safe_to_pay),
        "affordability_status": dec.affordability_status,
        "recommended_payment_method": dec.recommended_payment_method,
        "payment_plan": dec.payment_plan,
        "earliest_date_for_full_payment": dec.earliest_date_for_full_payment.isoformat() if dec.earliest_date_for_full_payment else "",
        "spending_changes_needed": dec.spending_changes_needed,
        "decision_explanation": dec.decision_explanation,
    }


def write_output(path: str, decisions: list[Decision]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, lineterminator="\n")
        w.writeheader()
        for d in decisions:
            w.writerow(decision_row(d))
