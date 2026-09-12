# Roadmap — Buy or Wait? submission

Status legend: [x] done · [ ] open · [~] partial

## Phase 0 — Understanding (done)
- [x] Read `problem_statement.md`, `AGENTS.md`, README; inspected every dataset schema and value distribution
- [x] Viewed all 16 images; recorded amounts + confidence in `code/cache/image_extractions.json`
- [x] Catalogued ~30 message scenario types (EN/ID) and their intended financial effect
- [x] `IMPLEMENTATION_NOTES.md` (schemas, date semantics, event statuses, option fields, ambiguities)
- [x] Decision gate 1 → hybrid with deterministic core (user decision)

## Phase 1 — Deterministic engine (done)
- [x] `ingest` (typed models, Decimal, ISO dates) · `fx` (dated rates, logged fallbacks)
- [x] `images` (sha256 cache → optional VLM → optional OCR; blank never zero)
- [x] `messages` (rule-based bilingual extraction; embedded instructions never executed)
- [x] `ledger` (cash-state classification, conflict rules, own-transfer detection, recurrence detection: monthly + interval sub-series, outlier trimming, income confirmation rules)
- [x] `forecast` (daily balance path, end-of-day semantics, payday funding, hybrid horizon)
- [x] `planner` (full / wait / partial / instalments / spending-change combos ≤3, exact re-simulation, contest ranking)
- [x] `explain` (fact-grounded templates) · `render` (exact CSV formatting) · `main.py` CLI (`--trace`, `--usage`, `--requests`)
- [x] Calibration vs 25 solved samples: 180-day phase replay, month M+2 monthly cut-off, midrange estimator, gross-figure salary messages
- [x] Decision gate 2 report; instalments+changes policy kept (user decision)

## Phase 2 — Evaluation, docs, packaging (done)
- [x] `evaluation/validate_output.py` (contract + legality + plan re-simulation) → VALID
- [x] `evaluation/evaluate_samples.py` → status 96 / method 96 / plan 92 / earliest 100 / changes 96 / amount within 5%: 88
- [x] 12 synthetic tests (`evaluation/tests`) covering the 11 required scenarios + formatting
- [x] `evaluation/usage_report.md` (final run: 0 model calls, 0 tokens, USD 0) + generator
- [x] `code/README.md`, `requirements.txt`, `evaluation/package_submission.py` → `code.zip` (26 files) verified from clean extraction
- [x] Decision gate 3 report; `output.csv` (250 rows); `log.txt` transcript (append-only)
- [x] Git: committed, merged to `main`, pushed to github.com/smishraa30/hackerrank-orchestrate-september26

## Phase 3 — Hardening & testing (done: 52 tests)
- [x] Message-parser unit tests: one test per scenario template, EN + ID variants, date/amount extraction, unclassified fallback
- [x] Recurrence-detection tests: two sub-series per category, missed occurrence → inactive, extra one-off inside a cadence, "Final employer payroll"
- [x] Horizon/phase tests: 180-day replay, monthly cut-off at month M+2, interval items to R+90, payday funding order
- [x] Ranking tests: every tie-breaker in isolation (deadline, changes, total, start, count, option id); partial vs wait; instalment vs wait
- [x] Spending-change tests: ≤3 changes, stop/reduce mutual exclusion, protected/non-permitted categories, `reduce_to` = minimum_allowed_amount, latest occurrence event id
- [x] Validator self-tests: fixtures with deliberate violations must be rejected (bad header, duplicate id, bad grammar, non-option instalment, illegal change, unsafe plan)
- [x] Property tests: amount_safe always in bounds; every recommended plan re-simulates safe; higher balance ⇒ amount_safe not lower; two runs byte-identical
- [x] Sample regression guard: fail if sample metrics drop below current thresholds
- [x] Graceful degradation tests: missing image file, missing rate pair, user with no history, deadline beyond horizon, empty messages/images files

## Phase 4 — Code quality (done)
- [x] Remove or clearly mark exploratory config switches (`interval_anchor` variants, `span_n`, `interval_first`) that are not the calibrated defaults
- [x] Lint (ruff/flake8) + type check pass; docstrings for public functions
- [x] CLI `--set key=value` overrides and `--version`; structured per-request assumption log (already in traces) surfaced as a summary file
- [x] Explanation consistency check in the validator (numbers quoted in the explanation match the output fields)

## Phase 5 — Decision-policy review (open, judgement calls)
- [x] Instalments + spending changes (23 rows) — kept (user decision)
- [ ] `wait` plans completing after the deadline (2 rows) → `affordable_later` vs `not_affordable`
- [ ] Telecom bill image: 822.05 (post-due-date) vs 704.05
- [ ] "Temporary"/"reduced" pay kept for all future payrolls vs reverting after the next payroll
- [ ] One-time arrears adjustments not projected (conservative)
- [ ] Optional LLM polish of `decision_explanation` behind an env flag (judged field) — only if cost/benefit is clear

## Phase 6 — Submission (open)
- [x] Rebuild `code.zip` after any code change (`python evaluation/package_submission.py`) and re-run validator + tests (done at 221cc94)
- [ ] Upload `code.zip`, `output.csv`, `log.txt` (chat_transcript) at
      https://www.hackerrank.com/contests/hackerrank-orchestrate-september26/challenges/buy-or-wait/submission
- [x] Final commit + push; tag the submitted revision (`submission-1`)
