# Architecture — Buy or Wait? decision engine

Deterministic, evidence-driven financial simulator and plan optimizer. No model call is on the decision
path; the optional vision model only fills blank amounts from images (and is off in the final run).

```mermaid
flowchart LR
    subgraph IN[dataset/]
        P[financial_profiles.csv]
        E[financial_events.csv]
        X[exchange_rates.csv]
        Q[requests.csv]
        O[request_payment_options.csv]
        M[messages.csv]
        I[images.csv + media/images/*.png]
    end

    IN --> ING[ingest.py<br/>typed models, Decimal, ISO dates]
    ING --> LED

    subgraph EV[evidence]
        FX[fx.py<br/>dated rate table<br/>exact → prior → later → inverse]
        IMG[images.py<br/>sha256 cache → optional VLM → optional OCR<br/>blank amount never zero]
        MSG[messages.py<br/>~30 bilingual rule templates → Amendment facts<br/>embedded instructions never executed]
    end
    EV --> LED

    LED[ledger.py · LedgerBuilder.build<br/>classify cash state · resolve conflicts<br/>detect recurrence (monthly / interval sub-series)<br/>apply amendments · provenance notes]
    LED --> FC[forecast.py<br/>series_occurrences · build_items<br/>balance_path (daily) · plan_is_safe<br/>amount_safe_today · earliest_full_payment_date]
    FC --> PL[planner.py · analyse<br/>candidates: full / wait / partial / instalment options / spending-change combos<br/>re-simulate every plan · rank_key tie-breakers · status_for]
    PL --> EXP[explain.py<br/>fact-grounded templates]
    PL --> REN[render.py<br/>exact CSV formatting]
    EXP --> REN
    REN --> OUT[(output.csv)]
    PL --> TR[trace JSON per request<br/>series, one-offs, candidates, rejections, provenance]

    subgraph EVAL[evaluation/]
        V[validate_output.py<br/>contract + legality + re-simulation + explanation consistency]
        S[evaluate_samples.py<br/>per-field accuracy vs solved samples]
        T[tests/ · 52 synthetic tests]
        U[write_usage_report.py → usage_report.md]
        Z[package_submission.py → code.zip]
    end
    OUT --> V
    OUT --> S
```

## 1. Ingest (`code/buyorwait/ingest.py`, `models.py`)
- Every CSV row becomes a frozen/typed dataclass (`Profile`, `Event`, `Request`, `PaymentOption`, `Message`, `ImageRef`).
- Money is `Decimal` (2 dp), dates are `datetime.date`; blank amounts stay `None`.
- `Dataset` indexes: events per user (sorted by settlement date), options per request (by option number),
  messages per user, images by `related_event_id`, rates by `(date, from, to)`.

## 2. Evidence layer
| Module | Responsibility | Fallback / safety |
|---|---|---|
| `fx.py` `RateTable` | Convert a foreign amount with the rate dated on its settlement date. | exact date → latest earlier → earliest later → inverse pair; missing pair raises `KeyError`, which the ledger turns into "row excluded, never guessed". Provenance string per conversion. |
| `images.py` `ImageExtractor` | Resolve a blank amount from its PNG. | sha256-keyed cache (`code/cache/image_extractions.json`, 16 verified entries) → Anthropic vision (`BUYORWAIT_ENABLE_VLM=1` + key, metered by `UsageMeter`) → pytesseract if installed → unresolved (row excluded with a note; never zero). |
| `messages.py` `interpret_message` | Turn a message into typed `Amendment` facts (salary amount/date, income stop, exclusions, one-off income, rent factor, keep-pending, retry-failed, no-ops). | Regex templates for every scenario in English and Indonesian; amounts/dates extracted structurally; unknown text → `unclassified` no-op; instruction-like content is never acted on. |

## 3. Ledger reconstruction (`ledger.py`)
`LedgerBuilder.build(request)` produces a `Ledger` with the opening balance, the minimum, active recurring
`Series`, dated `CashItem` one-offs, explicit-row dates per series key, and notes/provenance.

1. **Amounts** — images fill blanks; foreign rows converted at settlement date.
2. **Amendments** — messages parsed once per user.
3. **Cash-state classification**
   - settled before request date → history (recurrence only; already in the balance)
   - pending debit → reserved on settlement date; pending credit → ignored
   - scheduled row → reserved/credited on its date and marked *explicit* (projection skipped within ±3 days)
   - failed → ignored unless a bank message says it will be retried and no scheduled retry row exists
   - cancelled / unrealized → ignored; "Possible duplicate card charge" ignored unless a dispute message keeps it
   - own-transfer pairs (message-flagged) excluded from recurrence
4. **Recurrence detection** per `(event_type, category, direction)`: up to three sub-series —
   monthly (dominant day-of-month, one per month) or interval (median gap 2–35 days, ≥60% regular gaps,
   extra events inside the cadence dropped). Amount: fixed if the last two are equal, else the midrange of the
   history (outliers trimmed). Activity: monthly ≤45 days since last, interval ≤2 periods. Income is projected
   only when fixed (or the mode covers ≥50%), never after a "Final … payroll" row.
5. **Scheduled income** confirms/creates the salary series; **amendments** then set amounts from a date,
   move the payroll day, stop income, drop secondary/variable income, add approved invoices, scale rent.

## 4. Forecast (`forecast.py`)
- **Window**: `request_date … request_date + 90`. Monthly templates are projected only for the request month
  and the two following calendar months; interval templates and dated rows run to the end of the window.
- **Interval phase**: the history covers the 180 days before the request; future occurrences are
  `first_occurrence + 180 + k·interval` (the generator restarts the same phase), see notes §5b.
- **Balance path**: end-of-day balances; income and expenses on the same day net out; a plan payment on a
  payday is funded by that day's income. Optional intraday modes exist (EXPERIMENTAL).
- `amount_safe_today` = lowest projected balance − minimum, clamped to `[0, requested]`.
- `earliest_full_payment_date` = first day D such that paying the full amount on D keeps every later day of the
  window ≥ minimum (baseline must be safe before D).
- `plan_is_safe` re-simulates any payment list (with spending changes applied to future occurrences).

## 5. Planner (`planner.py`)
Candidates (only if the user accepts the method):
- **full_payment** today (safe ⇔ `amount_safe == requested`);
- **wait** — one full payment on the earliest date (may be after the deadline; ranked last);
- **partial_payment** — exactly `[request_date: amount_safe, earliest: remainder]`, request must allow it,
  `0 < safe < requested`, earliest ≤ deadline;
- **installments** — each supplied option reproduced exactly, `number_of_payments ≤ max_installment_months`,
  last payment inside the window, re-simulated;
- **spending changes** — only when no change-free plan completes by the deadline: enumerate `stop` /
  `reduce_to(minimum_allowed_amount)` on permitted, non-protected flexible series (≤3, never both on one
  series), ordered by total reduction; first combination that makes full payment (or the cheapest eligible
  instalment option) safe wins; changes reference the latest historical occurrence id.

Ranking (`rank_key`): completes by deadline → no changes → lowest total paid → earlier start → fewer payments
→ lowest option id. `status_for` maps the winner to the status/method pair; no candidate → `not_affordable` /
`not_recommended` (earliest date still reported when it exists).

## 6. Output (`explain.py`, `render.py`, `pipeline.py`, `main.py`)
- Explanations are rendered from ledger facts (minimum, lowest balance and date, next income, reserved
  one-offs, why alternatives lost).
- `render.py` formats `amount_safe_to_pay` minimally (`603.3`, `737`) and plan / `reduce_to` amounts with two
  decimals unless integral (`620.40`, `25256`).
- `Engine.decide` returns `Decision` (+ optional JSON trace); `main.py` runs all requests, writes `output.csv`,
  a usage JSON, per-request traces, supports `--requests`, `--set key=value`, `--version`, and writes a
  conservative fallback row if a request raises.

## 7. Configuration (`config.py`)
`EngineConfig` holds the calibrated defaults (horizon `hybrid`, estimator `midrange`, interval anchor `shift`
with 180 days, intraday `eod`, whole-window safety rule, change methods full+instalments). Rejected calibration
alternatives are kept as EXPERIMENTAL switches for reproducibility.

## 8. Evaluation & delivery (`evaluation/`)
- `validate_output.py` — header/order, one row per request, enums, grammar, bounds, partial exactness,
  instalment ↔ option match, spending-change legality, status/method consistency, explanation consistency,
  and a re-simulation of every recommended plan. Exit 0 = VALID.
- `evaluate_samples.py` — per-field accuracy and mismatches on the 25 solved samples.
- `tests/` — 52 synthetic tests (parser, recurrence, forecast, ranking, changes, validator self-tests,
  properties, regression guard).
- `write_usage_report.py` — `usage_report.md` from the run's usage JSON (0 model calls in the final run).
- `package_submission.py` — builds `code.zip` (code + evaluation + docs; no dataset/media/caches/secrets).

## 9. Provenance & logging
Every derived fact carries a note (image field used, rate date, message rule, series statistics, rejected
candidates) available via `--trace`. Development decisions are recorded in `IMPLEMENTATION_NOTES.md`
(schemas, inferred ground-truth rules, assumptions) and the append-only `log.txt` transcript.
