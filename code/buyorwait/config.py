"""Engine configuration.

Defaults are the values calibrated against the solved samples (see IMPLEMENTATION_NOTES.md section 5b).
Switches marked EXPERIMENTAL are alternatives that were evaluated and rejected during calibration; they
are kept so the comparison in the notes can be reproduced (`python code/main.py --set key=value`).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EngineConfig:
    horizon_days: int = 90
    # "days": window = [request_date, request_date + horizon_days];
    # "calendar": window ends on the last day of the second month after the request month;
    # "hybrid": window = request_date + horizon_days, but monthly templates are only projected for the
    #           request month and the two following calendar months (interval templates and dated rows run
    #           to the end of the window)
    horizon_mode: str = "hybrid"
    # apply projected recurring occurrences that fall exactly on request_date
    include_request_date: bool = True
    # amount estimator for variable recurring series: midrange (calibrated) | EXPERIMENTAL: mean, median, last, max, mean3, trimmed_mean
    estimator: str = "midrange"
    # intraday check: "eod" (end-of-day balances, calibrated) | EXPERIMENTAL: "debits_first" (all forecast debits before
    # credits), "interval_first" (interval-cadence and pending debits before credits; monthly debits after credits)
    intraday: str = "eod"
    # income is projected only when the amount is fixed (identical recent occurrences)
    income_requires_fixed_amount: bool = True
    # reserve pending debits on their settlement date
    include_pending_debits: bool = True
    # keep "Possible duplicate card charge" rows only when a message says the dispute is open
    duplicate_default_keep: bool = False
    # minimum number of occurrences to accept a recurring series
    min_occurrences: int = 2
    # monthly series inactive if request_date - last occurrence exceeds this many days
    monthly_inactive_days: int = 45
    # interval series inactive if gap since last occurrence exceeds factor * period
    interval_inactive_factor: float = 2.0
    # interval-series anchor: "last" (last occurrence + k*interval), "request" (request_date + k*interval, k>=1),
    # "request0" (request_date + k*interval, k>=0)
    # "shift": replay the history phase shifted by `history_shift_days` (first occurrence + shift + k*interval; calibrated)
    # EXPERIMENTAL: "last" (last occurrence + k*interval), "request" (request_date + k*interval, k>=1), "request0" (k>=0)
    interval_anchor: str = "shift"
    history_shift_days: int = 180
    # cadence estimate for interval series: "median_gap" (calibrated) | EXPERIMENTAL: "span_n" ((last-first)/n), "span_n1"
    interval_cadence: str = "median_gap"
    # optional remapping of detected interval lengths (e.g. {21: 14}); empty = keep detected cadence
    interval_remap: dict = field(default_factory=dict)
    # rounding of fractional projected dates: floor | round | ceil
    interval_rounding: str = "round"
    # minimum interval (days) accepted for an interval series; shorter cadences are treated as one-offs
    min_interval_days: int = 2
    # tolerance (days) to match an explicit scheduled event against a projected occurrence
    explicit_match_days: int = 3
    # outlier trimming for amount estimation (ratio to median)
    outlier_hi: float = 2.5
    outlier_lo: float = 0.35
    # safety horizon for a payment on day D: "window" (every later day of the window, calibrated) | EXPERIMENTAL:
    # "pay_period" (from D until the day before the next projected income; only D when no income follows)
    earliest_rule: str = "window"
    plan_rule: str = "window"
    # ranking: treat plans finishing after desired_completion_date as candidates (ranked last)
    allow_late_plans: bool = True
    # spending-change plans are generated with these methods
    change_methods: tuple[str, ...] = ("full_payment", "installments")  # partial excluded: its amounts are defined change-free
    notes: list[str] = field(default_factory=list)
