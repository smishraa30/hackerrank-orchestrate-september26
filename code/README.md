# Buy or Wait? — deterministic financial decision agent

Solution for the HackerRank Orchestrate "Buy or Wait?" challenge. For every row of
`dataset/requests.csv` the engine reconstructs the user's cash position from the supplied CSVs,
messages and images, forecasts the next 90 days, generates and verifies every eligible payment
plan, ranks them with the contest tie-breakers and writes `output.csv` with the exact required schema.

The final decision is never produced by free-form model reasoning: the finance engine is authoritative.
Model calls are optional (image extraction only), off by default, and metered into
`evaluation/usage_report.md`.

## Setup

- Python 3.10+ (developed on 3.14). No third-party package is required for the final run.
- Optional: `pip install -r requirements.txt` (pytest for the tests; `anthropic` only if you enable the
  vision extraction path).

```bash
git clone <repo>
cd <repo>
pip install -r requirements.txt   # optional
```

## Run (reproducible, offline, deterministic)

From the repository root:

```bash
python code/main.py
```

- reads everything from `dataset/` (profiles, events, rates, requests, payment options, messages, images);
- writes `output.csv` in the repository root (250 rows + header, ~1 s).

Useful flags:

```bash
python code/main.py --requests dataset/sample_requests.csv --output samples_out.csv   # run the solved samples
python code/main.py --trace traces/                                                  # one JSON trace per request
python code/main.py --usage evaluation/usage_run.json                                # token/cost summary (JSON)
python code/main.py --dataset /path/to/dataset --output /path/to/output.csv          # other locations
python code/main.py --set intraday=debits_first --set horizon_days=120                # override any EngineConfig field
```

## Evaluate

```bash
python evaluation/validate_output.py               # contract checks + re-simulation of every plan (exit 0 = valid)
python evaluation/evaluate_samples.py              # per-field accuracy on dataset/sample_requests.csv
python -m pytest evaluation/tests -q               # 52 synthetic unit/property/validator tests (+ a sample regression guard)
python -m ruff check code evaluation               # lint (config in ruff.toml)
python evaluation/write_usage_report.py --usage evaluation/usage_run.json   # regenerate usage_report.md
```

## How it works

Pipeline (`code/buyorwait/`): `ingest` → `fx` (dated rates) → `images` (cache / optional VLM / OCR) →
`messages` (bilingual rule-based fact extraction) → `ledger` (conflict resolution, recurrence detection)
→ `forecast` (daily balance path) → `planner` (candidates, safety checks, ranking, spending changes)
→ `explain` → `render`.

Key rules implemented (see `IMPLEMENTATION_NOTES.md` for the evidence behind each):

- Cash moves on `settlement_date`; settled rows before `request_date` are already in the balance and are
  used only to detect recurrence. Pending debits are reserved; pending credits, refunds, cancelled,
  failed, duplicate and unrealized rows are ignored (a bank message about an open dispute keeps the
  disputed charge reserved; a scheduled retry replaces a failed debit).
- Blank amounts are resolved from the linked image (never zero); every extraction carries provenance.
- Foreign-currency rows are converted with the rate dated on their settlement date.
- Recurrence: monthly templates (same day-of-month) and interval templates (5/7/10/14/21 days). Variable
  amounts are forecast with the midrange of the observed history (uniform noise around a base amount);
  fixed amounts are kept. Interval templates repeat the observed 180-day phase; monthly templates are
  projected for the request month and the two following months; the window is 90 days.
- Income is projected only when confirmed: fixed recurring payroll, a scheduled `Next confirmed salary`
  row, or an employer message (first salary, salary resumes, increase, date change, foreign-currency
  salary). Variable gig/freelance/commission income, bonuses, pending payouts, ended contracts and ended
  household incomes are not projected. Approved invoices are counted once on their settlement date.
- `amount_safe_to_pay` = the lowest projected balance minus `minimum_balance_to_keep` (clamped to
  `[0, requested_amount]`). `earliest_date_for_full_payment` = the first day on which one full payment
  keeps every later day of the window above the minimum (payday payments are funded by that day's income).
- Candidates: full payment today, wait (earliest date), partial (exactly two payments, second on the
  earliest date, on/before the deadline), each supplied instalment option (schedule reproduced exactly,
  `number_of_payments <= max_installment_months`), and — only when no change-free plan completes by the
  deadline — the smallest legal spending-change combination (≤ 3 flexible, permitted, non-protected
  series; `stop` or `reduce_to` the event's `minimum_allowed_amount`; referenced by the latest occurrence
  `event_id`) that makes full payment or an instalment plan safe. Every plan is re-simulated.
- Ranking: complete by deadline → no spending changes → lowest total paid → earlier start → fewer
  payments → lowest `payment_option_id`.

## Configuration

`code/buyorwait/config.py` holds every structural switch (horizon mode, estimator, phase anchoring,
intraday ordering…) with comments; defaults are the calibrated values. Environment variables (all
optional): `BUYORWAIT_ENABLE_VLM=1` + `ANTHROPIC_API_KEY` (vision extraction for images missing from the
cache), `BUYORWAIT_VLM_MODEL`, `BUYORWAIT_PRICE_IN_PER_MTOK`, `BUYORWAIT_PRICE_OUT_PER_MTOK`. Secrets are
read from the environment only; nothing is stored in the repository.

## Package layout

```text
code/main.py                 CLI entry point
code/buyorwait/              engine modules (see above)
code/cache/image_extractions.json   verified image amounts keyed by sha256 (16 entries)
evaluation/validate_output.py       contract validator + plan re-simulation
evaluation/evaluate_samples.py      sample scoring
evaluation/write_usage_report.py    usage report generator
evaluation/usage_report.md          final-run token/cost report
evaluation/tests/                   synthetic tests
IMPLEMENTATION_NOTES.md             schemas, inferred ground-truth behaviour, assumptions
ARCHITECTURE.md                     module-by-module architecture and data flow
```

## Known limitations

- Variable recurring amounts are estimates (midrange of history); decisions that sit within ~1% of the
  minimum-balance boundary can flip relative to the hidden ground truth.
- Messages are parsed with bilingual templates; an unseen phrasing falls back to "no structured fact"
  (logged in the trace), never to an invented fact.
- Without the cache, the VLM key or a local Tesseract install, an image-backed amount stays unresolved
  and the row is excluded from the forecast with a warning (it is never treated as zero).
- A foreign-currency row with no supplied rate is excluded (income is never guessed); a request that
  raises an unexpected error still gets a conservative `not_affordable` row and is listed on stderr.
