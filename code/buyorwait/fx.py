"""Fixed, dated exchange-rate conversion. Only supplied rates are used."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

CENT = Decimal("0.01")


class RateTable:
    def __init__(self, rates: dict[tuple[date, str, str], Decimal]):
        self.rates = rates
        self._by_pair: dict[tuple[str, str], list[tuple[date, Decimal]]] = {}
        for (d, f, t), r in rates.items():
            self._by_pair.setdefault((f, t), []).append((d, r))
        for lst in self._by_pair.values():
            lst.sort()
        self.assumptions: list[str] = []

    def rate(self, day: date, from_ccy: str, to_ccy: str) -> tuple[Optional[Decimal], str]:
        """Return (rate, provenance). Exact date first; otherwise latest earlier date,
        then earliest later date; inverse pair as a last resort."""
        if from_ccy == to_ccy:
            return Decimal(1), "identity"
        key = (day, from_ccy, to_ccy)
        if key in self.rates:
            return self.rates[key], f"exact:{day}:{from_ccy}->{to_ccy}"
        lst = self._by_pair.get((from_ccy, to_ccy), [])
        if lst:
            earlier = [x for x in lst if x[0] <= day]
            if earlier:
                d, r = earlier[-1]
                return r, f"latest-prior:{d}:{from_ccy}->{to_ccy}"
            d, r = lst[0]
            return r, f"earliest-later:{d}:{from_ccy}->{to_ccy}"
        inv = self._by_pair.get((to_ccy, from_ccy), [])
        if inv:
            if (day, to_ccy, from_ccy) in self.rates:
                r = self.rates[(day, to_ccy, from_ccy)]
                return (Decimal(1) / r), f"inverse-exact:{day}:{to_ccy}->{from_ccy}"
            earlier = [x for x in inv if x[0] <= day]
            d, r = earlier[-1] if earlier else inv[0]
            return (Decimal(1) / r), f"inverse-nearest:{d}:{to_ccy}->{from_ccy}"
        return None, "missing"

    def convert(self, amount: Decimal, day: date, from_ccy: str, to_ccy: str) -> tuple[Decimal, str]:
        r, prov = self.rate(day, from_ccy, to_ccy)
        if r is None:
            raise KeyError(f"no exchange rate for {from_ccy}->{to_ccy} on {day}")
        return (amount * r).quantize(CENT), prov
