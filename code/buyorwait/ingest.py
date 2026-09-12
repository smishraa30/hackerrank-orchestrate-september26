"""CSV ingestion into typed models. Deterministic ordering, exact decimals."""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from .models import Event, ImageRef, Message, PaymentOption, Profile, Request, SampleExpectation

OUTPUT_COLUMNS = [
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]


def parse_decimal(text: str) -> Optional[Decimal]:
    text = (text or "").strip()
    if text == "":
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise ValueError(f"bad decimal {text!r}") from exc


def parse_date(text: str) -> Optional[date]:
    text = (text or "").strip()
    if not text:
        return None
    return date.fromisoformat(text[:10])


def parse_list(text: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in (text or "").split("|") if x.strip())


def parse_bool(text: str) -> bool:
    return (text or "").strip().lower() in ("true", "1", "yes", "y")


def _read(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


@dataclass
class Dataset:
    root: str
    profiles: dict[str, Profile]
    events: list[Event]
    events_by_user: dict[str, list[Event]]
    events_by_id: dict[str, Event]
    rates: dict[tuple[date, str, str], Decimal]
    requests: list[Request]
    samples: list[Request]
    sample_expectations: dict[str, SampleExpectation]
    options_by_request: dict[str, list[PaymentOption]]
    messages_by_user: dict[str, list[Message]]
    images: list[ImageRef]
    images_by_event: dict[str, ImageRef]
    warnings: list[str] = field(default_factory=list)

    def image_path(self, image_id: str) -> str:
        return os.path.join(self.root, "media", "images", f"{image_id}.png")


def load_dataset(root: str) -> Dataset:
    warnings: list[str] = []

    profiles: dict[str, Profile] = {}
    for r in _read(os.path.join(root, "financial_profiles.csv")):
        mim = (r.get("max_installment_months") or "").strip()
        profiles[r["user_id"]] = Profile(
            user_id=r["user_id"],
            home_currency=r["home_currency"].strip(),
            current_available_balance=parse_decimal(r["current_available_balance"]) or Decimal(0),
            minimum_balance_to_keep=parse_decimal(r["minimum_balance_to_keep"]) or Decimal(0),
            financial_priorities=parse_list(r.get("financial_priorities", "")),
            protect=parse_list(r.get("expense_categories_to_protect", "")),
            reduce=parse_list(r.get("expense_categories_user_is_willing_to_reduce", "")),
            stop=parse_list(r.get("expense_categories_user_is_willing_to_stop", "")),
            methods=parse_list(r.get("payment_methods_user_will_consider", "")),
            max_installment_months=int(mim) if mim else None,
        )

    events: list[Event] = []
    for r in _read(os.path.join(root, "financial_events.csv")):
        events.append(
            Event(
                event_id=r["event_id"],
                user_id=r["user_id"],
                event_type=r["event_type"].strip(),
                description=r.get("description", "").strip(),
                category=r["category"].strip(),
                direction=r["direction"].strip(),
                amount=parse_decimal(r.get("amount", "")),
                currency=r["currency"].strip(),
                event_date=parse_date(r.get("event_date", "")),
                settlement_date=parse_date(r.get("settlement_date", "")),
                status=r["status"].strip(),
                linked_event_id=(r.get("linked_event_id") or "").strip(),
                flexibility=(r.get("flexibility") or "fixed").strip(),
                minimum_allowed_amount=parse_decimal(r.get("minimum_allowed_amount", "")),
            )
        )
    events_by_user: dict[str, list[Event]] = {}
    for e in events:
        events_by_user.setdefault(e.user_id, []).append(e)
    for lst in events_by_user.values():
        lst.sort(key=lambda e: (e.settlement_date or e.event_date or date.min, _event_number(e.event_id)))
    events_by_id = {e.event_id: e for e in events}

    rates: dict[tuple[date, str, str], Decimal] = {}
    for r in _read(os.path.join(root, "exchange_rates.csv")):
        d = parse_date(r["rate_date"])
        if d is None:
            continue
        rates[(d, r["from_currency"].strip(), r["to_currency"].strip())] = parse_decimal(r["rate"]) or Decimal(0)

    def _req(r: dict) -> Request:
        return Request(
            request_id=r["request_id"],
            user_id=r["user_id"],
            request_date=parse_date(r["request_date"]) or date.min,
            request_type=r.get("request_type", "").strip(),
            requested_amount=parse_decimal(r["requested_amount"]) or Decimal(0),
            desired_completion_date=parse_date(r["desired_completion_date"]) or date.max,
            allows_partial_payment=parse_bool(r.get("allows_partial_payment", "")),
            request_text=r.get("request_text", ""),
        )

    requests = [_req(r) for r in _read(os.path.join(root, "requests.csv"))]
    samples: list[Request] = []
    sample_expectations: dict[str, SampleExpectation] = {}
    sample_path = os.path.join(root, "sample_requests.csv")
    if os.path.exists(sample_path):
        for r in _read(sample_path):
            samples.append(_req(r))
            sample_expectations[r["request_id"]] = SampleExpectation(
                amount_safe_to_pay=r.get("amount_safe_to_pay", ""),
                affordability_status=r.get("affordability_status", ""),
                recommended_payment_method=r.get("recommended_payment_method", ""),
                payment_plan=r.get("payment_plan", ""),
                earliest_date_for_full_payment=r.get("earliest_date_for_full_payment", ""),
                spending_changes_needed=r.get("spending_changes_needed", ""),
                decision_explanation=r.get("decision_explanation", ""),
            )

    options_by_request: dict[str, list[PaymentOption]] = {}
    for r in _read(os.path.join(root, "request_payment_options.csv")):
        freq = (r.get("payment_frequency_days") or "").strip()
        opt = PaymentOption(
            payment_option_id=r["payment_option_id"],
            request_id=r["request_id"],
            payment_method=r["payment_method"].strip(),
            payment_amount=parse_decimal(r["payment_amount"]) or Decimal(0),
            number_of_payments=int(r["number_of_payments"]),
            first_payment_date=parse_date(r["first_payment_date"]) or date.min,
            payment_frequency_days=int(freq) if freq else None,
            financing_fee=parse_decimal(r.get("financing_fee", "")) or Decimal(0),
            total_payable_amount=parse_decimal(r["total_payable_amount"]) or Decimal(0),
        )
        options_by_request.setdefault(opt.request_id, []).append(opt)
    for lst in options_by_request.values():
        lst.sort(key=lambda o: o.option_number)

    messages_by_user: dict[str, list[Message]] = {}
    msg_path = os.path.join(root, "messages.csv")
    if os.path.exists(msg_path):
        for r in _read(msg_path):
            sent = r.get("sent_at", "")
            try:
                sent_d = datetime.fromisoformat(sent.replace("Z", "+00:00")).date() if sent else date.min
            except ValueError:
                sent_d = parse_date(sent) or date.min
            m = Message(
                message_id=r["message_id"],
                user_id=r["user_id"],
                request_id=(r.get("request_id") or "").strip(),
                related_event_id=(r.get("related_event_id") or "").strip(),
                sent_at=sent_d,
                source_type=(r.get("source_type") or "").strip(),
                message_text=r.get("message_text", ""),
            )
            messages_by_user.setdefault(m.user_id, []).append(m)
    for lst in messages_by_user.values():
        lst.sort(key=lambda m: (m.sent_at, m.message_id))

    images: list[ImageRef] = []
    img_path = os.path.join(root, "images.csv")
    if os.path.exists(img_path):
        for r in _read(img_path):
            images.append(
                ImageRef(
                    image_id=r["image_id"],
                    user_id=r["user_id"],
                    request_id=(r.get("request_id") or "").strip(),
                    related_event_id=(r.get("related_event_id") or "").strip(),
                )
            )
    images_by_event = {i.related_event_id: i for i in images if i.related_event_id}

    return Dataset(
        root=root,
        profiles=profiles,
        events=events,
        events_by_user=events_by_user,
        events_by_id=events_by_id,
        rates=rates,
        requests=requests,
        samples=samples,
        sample_expectations=sample_expectations,
        options_by_request=options_by_request,
        messages_by_user=messages_by_user,
        images=images,
        images_by_event=images_by_event,
        warnings=warnings,
    )


def _event_number(event_id: str) -> int:
    try:
        return int(event_id.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return 0
