"""Synthetic mini-dataset builder for unit/property tests (no real dataset needed)."""
from __future__ import annotations

import csv
import os
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "code"))

import pytest  # noqa: E402

PROFILE_COLS = ["user_id", "home_currency", "current_available_balance", "minimum_balance_to_keep", "financial_priorities",
                "expense_categories_to_protect", "expense_categories_user_is_willing_to_reduce",
                "expense_categories_user_is_willing_to_stop", "payment_methods_user_will_consider", "max_installment_months"]
EVENT_COLS = ["event_id", "user_id", "event_type", "description", "category", "direction", "amount", "currency", "event_date",
              "settlement_date", "status", "linked_event_id", "flexibility", "minimum_allowed_amount"]
REQ_COLS = ["request_id", "user_id", "request_date", "request_type", "requested_amount", "desired_completion_date",
            "allows_partial_payment", "request_text"]
OPT_COLS = ["payment_option_id", "request_id", "payment_method", "payment_amount", "number_of_payments", "first_payment_date",
            "payment_frequency_days", "financing_fee", "total_payable_amount"]
MSG_COLS = ["message_id", "user_id", "request_id", "related_event_id", "sent_at", "source_type", "message_text"]
IMG_COLS = ["image_id", "user_id", "request_id", "related_event_id"]
RATE_COLS = ["rate_date", "from_currency", "to_currency", "rate"]


class MiniData:
    """Accumulates rows and writes a dataset directory."""

    def __init__(self, root: str):
        self.root = root
        self.profiles: list[dict] = []
        self.events: list[dict] = []
        self.requests: list[dict] = []
        self.options: list[dict] = []
        self.messages: list[dict] = []
        self.images: list[dict] = []
        self.rates: list[dict] = []
        self._n = 0

    def profile(self, user_id="user_1", currency="EUR", balance="3000", minimum="1000", protect="rent", reduce="dining",
                stop="streaming", methods="full_payment|partial_payment|installments", max_months="6", priorities="emergency_savings"):
        self.profiles.append(dict(user_id=user_id, home_currency=currency, current_available_balance=balance,
                                  minimum_balance_to_keep=minimum, financial_priorities=priorities,
                                  expense_categories_to_protect=protect, expense_categories_user_is_willing_to_reduce=reduce,
                                  expense_categories_user_is_willing_to_stop=stop, payment_methods_user_will_consider=methods,
                                  max_installment_months=max_months))

    def event(self, **kw):
        self._n += 1
        row = dict(event_id=kw.pop("event_id", f"event_{self._n}"), user_id="user_1", event_type="expense", description="x",
                   category="groceries", direction="debit", amount="", currency="EUR", event_date="", settlement_date="",
                   status="settled", linked_event_id="", flexibility="fixed", minimum_allowed_amount="")
        row.update(kw)
        if not row["settlement_date"]:
            row["settlement_date"] = row["event_date"]
        self.events.append(row)
        return row["event_id"]

    def monthly(self, day: int, amount, months_before: int, request_date: date, category="rent", event_type="expense",
                description="Rent", flexibility="fixed", min_allowed="", direction="debit", currency="EUR", user_id="user_1"):
        ids = []
        y, m = request_date.year, request_date.month
        for k in range(months_before, -1, -1):  # includes the request month when the day is already past
            mm = m - k
            yy = y
            while mm <= 0:
                mm += 12
                yy -= 1
            import calendar as _cal

            d = date(yy, mm, min(day, _cal.monthrange(yy, mm)[1]))  # clamp like a real month-end bill
            if d >= request_date:
                continue
            ids.append(self.event(user_id=user_id, event_type=event_type, description=description, category=category,
                                  direction=direction, amount=str(amount), currency=currency, event_date=d.isoformat(),
                                  flexibility=flexibility, minimum_allowed_amount=min_allowed))
        return ids

    def interval(self, interval: int, amounts: list, request_date: date, category="groceries", offset=3, description="Shop",
                 flexibility="fixed", min_allowed="", user_id="user_1", currency="EUR"):
        start = request_date - timedelta(days=180) + timedelta(days=offset)
        d = start
        ids = []
        i = 0
        while d < request_date:
            ids.append(self.event(user_id=user_id, description=description, category=category, amount=str(amounts[i % len(amounts)]),
                                  currency=currency, event_date=d.isoformat(), flexibility=flexibility, minimum_allowed_amount=min_allowed))
            d += timedelta(days=interval)
            i += 1
        return ids

    def request(self, request_id="request_1", user_id="user_1", request_date="2026-03-05", amount="500",
                deadline="2026-04-20", allows_partial="true", rtype="purchase", text="Can I afford this?"):
        self.requests.append(dict(request_id=request_id, user_id=user_id, request_date=request_date, request_type=rtype,
                                  requested_amount=amount, desired_completion_date=deadline, allows_partial_payment=allows_partial,
                                  request_text=text))

    def option(self, option_id, request_id, method, amount, n, first, freq, fee, total):
        self.options.append(dict(payment_option_id=option_id, request_id=request_id, payment_method=method, payment_amount=amount,
                                 number_of_payments=n, first_payment_date=first, payment_frequency_days=freq, financing_fee=fee,
                                 total_payable_amount=total))

    def message(self, text, message_id="message_1", user_id="user_1", request_id="", related="", sent="2026-03-01T09:30:00Z", source="employer"):
        self.messages.append(dict(message_id=message_id, user_id=user_id, request_id=request_id, related_event_id=related,
                                  sent_at=sent, source_type=source, message_text=text))

    def image(self, image_id, related, user_id="user_1", request_id="request_1"):
        self.images.append(dict(image_id=image_id, user_id=user_id, request_id=request_id, related_event_id=related))

    def rate(self, day, frm, to, rate):
        self.rates.append(dict(rate_date=day, from_currency=frm, to_currency=to, rate=rate))

    def write(self) -> str:
        os.makedirs(os.path.join(self.root, "media", "images"), exist_ok=True)

        def dump(name, cols, rows):
            with open(os.path.join(self.root, name), "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
                w.writeheader()
                for r in rows:
                    w.writerow({c: r.get(c, "") for c in cols})

        dump("financial_profiles.csv", PROFILE_COLS, self.profiles)
        dump("financial_events.csv", EVENT_COLS, self.events)
        dump("requests.csv", REQ_COLS, self.requests)
        dump("request_payment_options.csv", OPT_COLS, self.options)
        dump("messages.csv", MSG_COLS, self.messages)
        dump("images.csv", IMG_COLS, self.images)
        dump("exchange_rates.csv", RATE_COLS, self.rates)
        return self.root


@pytest.fixture
def mini(tmp_path):
    return MiniData(str(tmp_path / "dataset"))


def run(md: MiniData, request_id="request_1", cfg=None):
    from buyorwait.config import EngineConfig
    from buyorwait.ingest import load_dataset
    from buyorwait.pipeline import Engine

    root = md.write()
    ds = load_dataset(root)
    engine = Engine(ds, cfg or EngineConfig())
    req = next(r for r in ds.requests if r.request_id == request_id)
    return engine.decide(req, with_trace=True)
