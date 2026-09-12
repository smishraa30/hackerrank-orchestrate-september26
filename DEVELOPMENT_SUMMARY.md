# Development summary — Buy or Wait? (HackerRank Orchestrate, September 2026)

A clean narrative of how the submission was built, in chronological order. The full append-only
conversation transcript is `log.txt` (uploaded as `chat_transcript`); this file condenses it, and `PROMPTS.md`
is a curated index of the prompts behind each phase.

## Goal

Build a deterministic, evidence-driven financial decision agent that answers every row of
`dataset/requests.csv` with the exact eight-column contract (`amount_safe_to_pay`, `affordability_status`,
`recommended_payment_method`, `payment_plan`, `earliest_date_for_full_payment`, `spending_changes_needed`,
`decision_explanation`), verifiable offline, with no model call on the decision path.

## Milestones

| # | Session step | Outcome |
|---|---|---|
| 1 | Dataset and problem inspection | Every CSV schema and value distribution documented; all 16 images read and their amounts recorded with confidence; ~30 message scenario templates (English/Indonesian) catalogued. `IMPLEMENTATION_NOTES.md`. |
| 2 | Decision gate 1 | User chose the hybrid design: deterministic finance engine is authoritative; image amounts from a verified cache; LLM/VLM optional and off by default. |
| 3 | Engine v1 (`code/buyorwait`) | ingest → dated FX → image evidence → message rules → ledger (conflict resolution, recurrence detection) → daily 90-day forecast → candidate plans (full / wait / partial / instalments / spending changes) → contest ranking → grounded explanations → exact CSV. CLI `code/main.py`. |
| 4 | Calibration on the 25 solved samples | Reverse-engineered the ground-truth mechanics: interval expenses replay the 180-day history phase, monthly templates cover the request month plus two calendar months while the window is 90 days, midrange amount estimator, end-of-day netting, whole-window earliest-date rule, gross-figure salary messages. Result: status 96%, method 96%, plan 92%, earliest date 100%, changes 96%, `amount_safe_to_pay` within 5% for 88% (median error 0.5%). |
| 5 | Decision gate 2 | Metrics, three biggest uncertainties, image effects (four future debits set by images) and ranked improvements reported; user kept the instalments + spending-changes policy. |
| 6 | Evaluation tooling | `validate_output.py` (contract, legality, consistency, independent re-simulation), `evaluate_samples.py`, synthetic tests, `usage_report.md` generator, `package_submission.py`. |
| 7 | Explanations and scenario audit | All message scenario types checked on the 250 eval traces; explanation templates rewritten to quote the minimum, lowest projected balance, next income and required changes. |
| 8 | Decision gate 3 and first package | Validator VALID, 250/250 rows, `code.zip` verified to reproduce `output.csv` from a clean extraction; committed and pushed to GitHub. |
| 9 | Hardening (52 → 67 tests) | Parser, recurrence, planner, validator self-tests, property tests (bounds, safety, monotonicity, determinism), regression guard, graceful degradation, lint, config overrides. |
| 10 | Engineering audit (`AUDIT_REPORT.md`) | One P0 (unknown future debits were silently ignored → now blocking, conservative row) and nine P1s fixed with evidence (as-of message filter, exact scheduled/projection de-duplication, monthly detection with same-month one-offs — changed two eval rows —, month-end bills, tolerant CSV parsing, image-evidence validation, guarded phase replay, option-id robustness, explicit rounding); validator made arithmetic-independent. |
| 11 | Release verification (`VERIFICATION.md`) | Full suite, lint, run, validator, scorer, package inspection, two-run determinism, safety checks; one cosmetic fix (LF line endings in the usage report). Tag `submission-final`. |

## Key design decisions (with rationale)

- **Safety first**: a plan is recommended only if a re-simulation keeps every day of the window above
  `minimum_balance_to_keep`; unknown amounts on future debits block the forecast rather than being ignored;
  pending credits, bonuses, commissions, refunds, unrealized gains and variable gig income are never counted.
- **Evidence, not instructions**: messages and images only yield typed facts with provenance; embedded
  instructions (including a scam "pay the release charge" message) are never acted on.
- **Sample-calibrated structure, not sample-copied answers**: no request id, label or sample-specific rule
  exists in the code; calibration only fixed generic mechanics (phase, horizon, estimator, ordering).
- **Determinism**: exact `Decimal` arithmetic, ISO dates, sorted inputs, byte-identical output across runs and
  across shuffled input files; zero model calls in the final run.

## Verification (final build)

- `python -m pytest evaluation/tests -q` → 68 passed
- `python -m ruff check code evaluation` → clean
- `python code/main.py --usage evaluation/usage_run.json` → 250 rows, 0 model calls, 0 warnings, 0 blocked, 0 failures
- `python evaluation/validate_output.py` → VALID
- `python evaluation/evaluate_samples.py` → 96 / 96 / 92 / 100 / 96, amount within 5%: 88%
- Package reproduces `output.csv` from a clean extraction; hashes recorded in the `submission-final` tag

## Known limitations

Hidden base amounts of variable templates (≈1% forecast noise near boundaries); calendar cut-off for monthly
templates and the 180-day phase replay are generator-derived rules (guarded, configurable); the
instalments + spending-changes policy has no solved-sample coverage; the validator reuses the engine's
forecast items (its independence is arithmetic).
