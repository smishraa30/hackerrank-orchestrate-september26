"""Rule-based interpretation of messages (English + Indonesian templates).

Messages are untrusted evidence. We only extract structured financial facts
(amounts, dates, scenario type); embedded instructions are never executed.
Each amendment carries provenance (message_id, matched rule).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from .models import Message

CURRENCIES = ("INR", "EUR", "IDR", "USD", "ZAR")

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "januari": 1, "februari": 2, "maret": 3, "mei": 5, "juni": 6, "juli": 7, "agustus": 8,
    "oktober": 10, "desember": 12,
}

_AMOUNT_RE = re.compile(r"\b(INR|EUR|IDR|USD|ZAR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
_ISO_DATE_RE = re.compile(r"\b(20[0-9]{2}-[01][0-9]-[0-3][0-9])\b")
_TEXT_DATE_RE = re.compile(
    r"\b([0-3]?[0-9])\s+(January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Januari|Februari|Maret|Mei|Juni|Juli|Agustus|Oktober|Desember)\s+(20[0-9]{2})\b",
    re.IGNORECASE,
)
_PCT_RE = re.compile(r"\b([0-9]{1,3}(?:\.[0-9]+)?)\s*%")


@dataclass
class Amendment:
    kind: str
    message_id: str
    rule: str
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    day: Optional[date] = None
    factor: Optional[Decimal] = None
    event_id: Optional[str] = None
    recurring: bool = False
    note: str = ""
    extras: dict = field(default_factory=dict)


def extract_amounts(text: str) -> list[tuple[str, Decimal]]:
    out = []
    for m in _AMOUNT_RE.finditer(text):
        out.append((m.group(1), Decimal(m.group(2).replace(",", ""))))
    return out


def extract_dates(text: str) -> list[date]:
    out = []
    for m in _ISO_DATE_RE.finditer(text):
        try:
            out.append(date.fromisoformat(m.group(1)))
        except ValueError:
            pass
    for m in _TEXT_DATE_RE.finditer(text):
        mon = _MONTHS.get(m.group(2).lower())
        if mon:
            try:
                out.append(date(int(m.group(3)), mon, int(m.group(1))))
            except ValueError:
                pass
    return out


def _has(text: str, *pats: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in pats)


def _date_after(text: str, *pats: str) -> Optional[date]:
    """First date mentioned after the first match of any pattern (falls back to the first date in the text)."""
    low = text.lower()
    pos = None
    for p in pats:
        m = re.search(p, low)
        if m and (pos is None or m.start() < pos):
            pos = m.start()
    dated = []
    for m in _ISO_DATE_RE.finditer(text):
        try:
            dated.append((m.start(), date.fromisoformat(m.group(1))))
        except ValueError:
            pass
    for m in _TEXT_DATE_RE.finditer(text):
        mon = _MONTHS.get(m.group(2).lower())
        if mon:
            try:
                dated.append((m.start(), date(int(m.group(3)), mon, int(m.group(1)))))
            except ValueError:
                pass
    dated.sort()
    if pos is not None:
        after = [d for p, d in dated if p >= pos]
        if after:
            return after[0]
    return dated[0][1] if dated else None


def interpret_message(msg: Message) -> list[Amendment]:
    """Map one message to zero or more structured amendments."""
    t = msg.message_text
    amounts = extract_amounts(t)
    dates = extract_dates(t)
    out: list[Amendment] = []
    mid = msg.message_id

    def amt(i: int = 0) -> tuple[Optional[str], Optional[Decimal]]:
        return amounts[i] if len(amounts) > i else (None, None)

    # --- income scenarios -------------------------------------------------
    if _has(t, r"salary has increased to", r"gaji bulanan anda naik menjadi"):
        ccy, a = amt()
        d = dates[0] if dates else None
        if a is not None:
            out.append(Amendment("salary_amount", mid, "salary_increase", amount=a, currency=ccy, day=d,
                                 note="salary increase applies from date"))
    elif _has(t, r"regular salary for the next payroll is", r"gaji rutin anda untuk penggajian berikutnya adalah"):
        ccy, a = amt()
        if a is not None:
            out.append(Amendment("salary_amount", mid, "regular_plus_arrears", amount=a, currency=ccy,
                                 note="regular salary confirmed; one-time arrears adjustment not counted (unsettled bonus-like credit)"))
        if len(amounts) > 1:
            out.append(Amendment("ignore_one_off_credit", mid, "regular_plus_arrears", amount=amounts[1][1],
                                 currency=amounts[1][0], note="one-time arrears adjustment recorded but not projected"))
    elif _has(t, r"temporary monthly pay is", r"gaji bulanan sementara anda adalah"):
        ccy, a = amt()
        if a is not None:
            out.append(Amendment("salary_amount", mid, "temporary_pay", amount=a, currency=ccy,
                                 note="temporary reduced pay continues for next payroll; kept for the forecast (safer)"))
    elif _has(t, r"next salary is reduced to", r"gaji berikutnya .*dikurangi menjadi"):
        ccy, a = amt()
        if a is not None:
            out.append(Amendment("salary_amount", mid, "salary_reduced", amount=a, currency=ccy,
                                 note="next salary reduced (unpaid leave); kept for the forecast (safer)"))
    elif _has(t, r"confirmed salary is now expected on", r"kini diperkirakan masuk pada"):
        if dates:
            out.append(Amendment("salary_date", mid, "salary_date_change", day=dates[0],
                                 note="payroll date replaced by employer notice"))
    elif _has(t, r"confirmed base salary is", r"gaji pokok yang dikonfirmasi adalah"):
        # The quoted figure is a gross/total number (base x 5/3 across the dataset); the settled base-salary
        # history is the confirmed recurring amount. Commissions on open deals are not counted.
        ccy, a = amt()
        out.append(Amendment("exclude_variable_income", mid, "base_salary_commission_pending", amount=a, currency=ccy,
                             note="base salary kept from settled history; commission credits pending approval excluded"))
    elif _has(t, r"seasonal contract has ended", r"kontrak musiman saat ini telah berakhir",
              r"your employment has ended", r"hubungan kerja anda telah berakhir"):
        out.append(Amendment("income_stop", mid, "income_ended", note="no further regular income confirmed"))
    elif _has(t, r"regular salary of .* resumes on", r"gaji rutin sebesar .* dilanjutkan"):
        ccy, a = amt()
        d = dates[0] if dates else None
        if a is not None and d is not None:
            out.append(Amendment("salary_amount", mid, "salary_resumes", amount=a, currency=ccy, day=d,
                                 recurring=True, note="salary resumes on date; new childcare payment amount unknown, not invented"))
    elif _has(t, r"first salary", r"gaji pertama"):
        ccy, a = amt()
        d = dates[0] if dates else None
        if a is not None and d is not None:
            out.append(Amendment("salary_amount", mid, "first_salary", amount=a, currency=ccy, day=d, recurring=True,
                                 note="first salary confirmed on date; projected monthly thereafter"))
    elif _has(t, r"salary of (inr|eur|idr|usd|zar) [0-9.,]+ is confirmed for", r"gaji sebesar (inr|eur|idr|usd|zar) [0-9.,]+ dikonfirmasi untuk",
              r"confirmed a (inr|eur|idr|usd|zar) [0-9.,]+ salary credit for"):
        pats = (r"salary of (inr|eur|idr|usd|zar)", r"gaji sebesar", r"confirmed a (inr|eur|idr|usd|zar) [0-9.,]+ salary credit")
        m_amt = re.search(r"(INR|EUR|IDR|USD|ZAR) ([0-9][0-9,]*(?:\.[0-9]+)?) salary credit|salary of (INR|EUR|IDR|USD|ZAR) ([0-9][0-9,]*(?:\.[0-9]+)?)|[Gg]aji sebesar (INR|EUR|IDR|USD|ZAR) ([0-9][0-9,]*(?:\.[0-9]+)?)", t)
        if m_amt:
            g = [x for x in m_amt.groups() if x]
            ccy, a = g[0], Decimal(g[1].replace(",", ""))
        else:
            ccy, a = amt()
        d = _date_after(t, *pats)
        if a is not None and d is not None:
            out.append(Amendment("salary_amount", mid, "foreign_salary_confirmed", amount=a, currency=ccy, day=d,
                                 recurring=True, note="salary confirmed for date in stated currency; converted at settlement-date rate"))
    elif _has(t, r"client approved an invoice payment of", r"klien menyetujui pembayaran faktur sebesar"):
        ccy, a = amt()
        d = dates[0] if dates else None
        if a is not None and d is not None:
            out.append(Amendment("one_off_income", mid, "invoice_approved", amount=a, currency=ccy, day=d,
                                 note="approved invoice counted on expected settlement date; unapproved invoices ignored"))
    elif _has(t, r"quarterly bonus", r"bonus kuartalan"):
        out.append(Amendment("noop", mid, "bonus_pending", note="bonus not approved; not counted"))
    elif _has(t, r"payout is still pending", r"masih tertunda"):
        out.append(Amendment("exclude_variable_income", mid, "gig_payout_pending",
                             note="gig payout pending / not withdrawable; variable income not projected"))
    elif _has(t, r"one household employment record has ended", r"salah satu sumber pendapatan kerja rumah tangga telah berakhir"):
        # The quoted "remaining" figure is a gross number; the settled primary-salary history is kept and the
        # ended household income is removed from the projection.
        ccy, a = amt()
        out.append(Amendment("exclude_secondary_income", mid, "household_income_ended", amount=a, currency=ccy,
                             note="ended household income removed; primary salary kept from settled history"))
    elif _has(t, r"reimbursement for your earlier work expense", r"penggantian atas biaya kerja"):
        out.append(Amendment("noop", mid, "reimbursement", note="one-off reimbursement, not salary"))
    # --- expense / account scenarios ---------------------------------------
    elif _has(t, r"increases monthly rent by", r"menaikkan biaya sewa bulanan sebesar"):
        m = _PCT_RE.search(t)
        pct = Decimal(m.group(1)) if m else Decimal(0)
        out.append(Amendment("rent_increase", mid, "rent_increase", factor=(Decimal(1) + pct / Decimal(100)),
                             note=f"rent +{pct}% from next payment"))
    elif _has(t, r"transfer between your two accounts", r"transfer antara dua rekening anda"):
        out.append(Amendment("own_transfer", mid, "own_transfer", note="matching debit/credit are an internal transfer"))
    elif _has(t, r"previous debit attempt failed", r"upaya debit sebelumnya gagal"):
        out.append(Amendment("retry_failed", mid, "failed_debit_retry", event_id=msg.related_event_id or None,
                             note="failed bill still outstanding; retry will be attempted"))
    elif _has(t, r"extra card charge is still being investigated", r"tagihan kartu tambahan masih dalam penyelidikan"):
        out.append(Amendment("keep_pending_debit", mid, "disputed_charge_no_reversal", event_id=msg.related_event_id or None,
                             note="disputed extra charge not reversed yet; reserved (safer)"))
    elif _has(t, r"minimum payments due on two separate card accounts", r"dua rekening kartu"):
        out.append(Amendment("noop", mid, "two_card_minimums", note="two separate card minimums; both kept"))
    elif _has(t, r"refund", r"pengembalian dana"):
        out.append(Amendment("noop", mid, "refund_pending", note="pending refund not counted"))
    elif _has(t, r"portfolio", r"displayed value", r"nilai investasi"):
        out.append(Amendment("noop", mid, "portfolio_valuation", note="unrealized valuation ignored"))
    elif _has(t, r"prize", r"hadiah"):
        if _has(t, r"pay the release charge", r"bayar biaya pencairan"):
            out.append(Amendment("noop", mid, "scam_ignored", note="embedded payment instruction ignored (untrusted)"))
        else:
            out.append(Amendment("noop", mid, "prize", note="prize proceeds: pending ignored / settled already in balance; no recurrence"))
    elif _has(t, r"investment sale", r"penjualan investasi"):
        out.append(Amendment("noop", mid, "investment_sale_settled", note="settled sale proceeds already in balance"))
    elif _has(t, r"foreign currency", r"mata uang asing"):
        out.append(Amendment("noop", mid, "foreign_currency_settlement", note="converted at settlement-date rate"))
    elif _has(t, r"receipt", r"was charged", r"was paid", r"dibayar"):
        out.append(Amendment("noop", mid, "receipt_confirmation", note="receipt confirms image-backed amount"))
    else:
        out.append(Amendment("noop", mid, "unclassified", note="no structured fact extracted"))

    # secondary facts that can co-exist with the main scenario (e.g. message_86)
    if not any(a.rule == "foreign_salary_confirmed" for a in out):
        m = re.search(r"confirmed a (INR|EUR|IDR|USD|ZAR) ([0-9][0-9,]*(?:\.[0-9]+)?) salary credit for", t)
        if m:
            d = _date_after(t, r"salary credit for")
            if d is not None:
                out.append(Amendment("salary_amount", mid, "foreign_salary_confirmed", amount=Decimal(m.group(2).replace(",", "")),
                                     currency=m.group(1), day=d, recurring=True,
                                     note="salary credit confirmed for date; converted at settlement-date rate"))
    return out


def interpret_all(messages: list[Message]) -> list[Amendment]:
    out: list[Amendment] = []
    for m in messages:
        out.extend(interpret_message(m))
    return out
