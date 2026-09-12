# Verification procedure — Buy or Wait? submission

Run every step from the repository root (`hackerrank-orchestrate-september26`). Each step lists the command,
what to look for, and the expected value at release tag `submission-3` (commit `ada7910`). No network access or
API key is needed at any point.

## 0. Prerequisites and repository state

```bash
python --version
```
Expected: Python 3.10 or newer (developed on 3.14). No third-party package is required; `pytest` and `ruff` are
only needed for steps 2–3 (`pip install pytest ruff`).

```bash
git status -sb && git log --oneline -1 && git tag -l
```
Expected: clean tree, `main` in sync with `origin/main`, latest commit `ada7910 fix(release): …`, tags
`submission-1`, `submission-2`, `submission-3`.

```bash
git diff --stat upstream/main -- dataset
```
Expected: no output — the organiser's dataset files are untouched (`upstream` = interviewstreet remote).

## 1. Tests (engine rules, validator self-tests, properties, audit regressions)

```bash
python -m pytest evaluation/tests -q
```
Expected: `68 passed`. The test files and what they prove:
`test_engine.py` (the 11 required scenarios + formatting), `test_messages.py` (every message template, EN/ID,
injected instructions ignored), `test_recurrence.py` (series detection, phase, horizon, payday netting),
`test_planner.py` (all six tie-breakers, spending-change rules), `test_validator_and_properties.py` (corrupted
outputs are rejected; bounds, safety, monotonicity, determinism; sample regression guard),
`test_audit.py` (as-of messages, unknown debits block, pending semantics, FX fallbacks, month-end, partial split
search, duplication, malformed CSVs, permutation invariance, image-cache validation, monotone safety).

## 2. Lint

```bash
python -m ruff check code evaluation
```
Expected: `All checks passed!`

## 3. Full run (the file you submit)

```bash
python code/main.py --usage evaluation/usage_run.json
```
Expected: `wrote 250 rows to …\output.csv in ~1 s; model calls: 0` and **no** lines starting with
`FAILURE`, `BLOCKED` or `dataset warning` on stderr.

```bash
python -c "import json;u=json.load(open('evaluation/usage_run.json'));print(u['requests'],u['total'],u['dataset_warnings'],u['requests_blocked_by_unknown_debits'],u['request_failures'])"
```
Expected: `250 {'calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'cost_usd': 0.0} [] [] []`

```bash
sha256sum output.csv
```
Expected: `a47a3441f2ae9d7735690d5474f7d69a0b144fcfb05434eaa819435f2f83f148`

## 4. Contract validation (independent re-simulation)

```bash
python evaluation/validate_output.py
```
Expected: `VALID: output passes all checks`. This checks the exact header/order, one row per request, enum
values, date/plan grammar, `0 <= amount_safe_to_pay <= requested_amount`, partial-plan exactness, instalment
plans against the supplied options, spending-change legality, status/method consistency, explanation
consistency, and re-simulates every plan with its own daily walk. To see it fail on purpose, edit one
`affordability_status` in a copy of `output.csv` and validate that copy with `--output <copy>`.

## 5. Sample scoring

```bash
python evaluation/evaluate_samples.py
```
Expected: `affordability_status 96%`, `recommended_payment_method 96%`, `payment_plan 92%`,
`earliest_date_for_full_payment 100%`, `spending_changes_needed 96%`, `amount_safe_to_pay within 5%: 88%`,
mismatches listed for request_06 (spending-change plan 0.6 EUR short) and request_19 (partial split 4% off).

## 6. Inspect the reasoning for a few requests (provenance)

```bash
python code/main.py --requests dataset/sample_requests.csv --output samples_out.csv --trace traces --quiet
```
Then open `traces/request_16.json` (image-backed rent balance, INR 100,000) and `traces/request_07.json`
(payroll date moved to the 23rd by an employer message). Look at `series`, `one_offs`, `provenance.images`,
`provenance.messages`, `candidates` and `rejected`. Compare `samples_out.csv` with the solved columns in
`dataset/sample_requests.csv`.

## 7. Determinism

```bash
python code/main.py --output run_a.csv --quiet && python code/main.py --output run_b.csv --quiet && sha256sum run_a.csv run_b.csv output.csv
```
Expected: three identical hashes (`a47a3441…3f148`). Delete `run_a.csv`, `run_b.csv`, `samples_out.csv` and
`traces/` afterwards (they are git-ignored scratch files).

## 8. Build and inspect the submission package

```bash
python evaluation/package_submission.py
```
Expected: `wrote …\code.zip: 35 files, ~102 KB`.

```bash
python evaluation/package_submission.py --list
```
Expected: only `README.md`, `code/**` (main.py, buyorwait/*.py, cache/image_extractions.json), `evaluation/**`
(scripts, usage_report.md, tests), `IMPLEMENTATION_NOTES.md`, `ARCHITECTURE.md`, `AUDIT_REPORT.md`,
`ROADMAP.md`, `requirements.txt`, `ruff.toml`. No `dataset/`, `media/`, `output.csv`, traces, caches, secrets.
`code/cache/image_extractions.json` is the verified image-evidence store (16 amounts, sha256-keyed, no
secrets); it is required to reproduce `output.csv`.

```bash
sha256sum code.zip
```
Expected: `b89db2d6bb9c7355e0998bf83acbfb224c726ae9ba96bec293e73e62df40adc1`

Reproduce from a clean extraction (any temporary folder):

```bash
mkdir ziptest && cd ziptest && python -c "import zipfile; zipfile.ZipFile('../code.zip').extractall('.')" && python code/main.py --dataset ../dataset --output out.csv --quiet && sha256sum out.csv && cd ..
```
Expected: the same `a47a3441…3f148` hash. Then remove `ziptest/`.

## 9. Usage report and transcript

```bash
cat evaluation/usage_report.md
```
Expected: final-run timestamp, 250 requests, 0 model calls / tokens / cost, zero-cost deterministic operations
listed, no keys.

```bash
grep -c "^## \[" log.txt && grep -c "SESSION START" log.txt && grep -ciE "sk-ant|api_key=|ANTHROPIC_API_KEY=" log.txt
```
Expected: entry count (14+), 2 session starts, and `0` key-shaped strings. `log.txt` is git-ignored and is the
`chat_transcript` upload.

## 10. Remote state

```bash
git fetch origin && git status -sb && git ls-remote --tags origin
```
Expected: `main...origin/main` (in sync) and the three `submission-*` tags on GitHub.

## 11. Submit

Upload exactly these three files at
https://www.hackerrank.com/contests/hackerrank-orchestrate-september26/challenges/buy-or-wait/submission :

| Upload field | File | sha256 |
|---|---|---|
| `code.zip` | `code.zip` | `b89db2d6bb9c7355e0998bf83acbfb224c726ae9ba96bec293e73e62df40adc1` |
| `output.csv` | `output.csv` | `a47a3441f2ae9d7735690d5474f7d69a0b144fcfb05434eaa819435f2f83f148` |
| `chat_transcript` | `log.txt` | changes with every logged turn — hash it right before uploading |

If any expected value above does not match, stop and re-run from step 1; do not edit `output.csv` by hand.
