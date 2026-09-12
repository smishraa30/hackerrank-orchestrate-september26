# Engineering audit — Buy or Wait? engine (2026-09-13)

Scope: architecture (`ARCHITECTURE.md`, treated as a design reference only), the `code/buyorwait` implementation,
`evaluation/` tooling, the dataset schemas and the 25 solved samples. Every finding below was confirmed by code
inspection plus a targeted test or a reproducible data check before any change was made.

Priority: **P0** can produce an unsafe/wrong financial decision · **P1** correctness/reliability · **P2** performance/maintainability.

## Confirmed findings and resolutions

| # | Pri | Finding (evidence) | Resolution |
|---|---|---|---|
| 1 | P0 | An image-backed **future debit** whose amount could not be resolved (cache miss, low confidence, missing rate, unparseable value) was silently *excluded* from the forecast, i.e. the recommendation became more optimistic (`ledger.build` `continue` paths; reproduced with a blank-amount scheduled bill → `affordable_now`). | `DataIssue` model; unknown/unconvertible future debits are **blocking**: the planner returns the conservative row (`amount_safe_to_pay=0`, `not_affordable`, `not_recommended`, explanation states the missing amount). Unknown historical rows and unknown credits are recorded as warnings (ignoring them is the safe direction). Test: `test_unknown_future_debit_never_makes_the_answer_more_optimistic`, `test_later_only_fx_rate_…`. |
| 2 | P1 | **Look-ahead**: every message of a user was applied regardless of `sent_at`. No dataset message is dated after its request, but the code had no as-of guard. | Messages with `sent_at > request_date` are ignored and recorded (`future_message_ignored`). Test: `test_message_sent_after_request_date_is_ignored`. |
| 3 | P1 | **Scheduled vs projected duplication** relied on a ±3-day window. Data check: all 47 `Next confirmed salary` rows sit exactly on the projected day (one on the 23rd after the series adopts that day), while scheduled utility/insurance/school rows are 1–3 days off the regular bill and are *extra* charges (sample request_24 fits only when both are counted). A ±3 window would wrongly drop those bills if they settled closer. | Suppression now requires an exact date match (settlement **or** event date) with an explicit row of the same series; scheduled non-salary credits are no longer counted (only confirmed salary rows are). Output unchanged. Test: `test_scheduled_rows_replace_projection_only_on_the_same_date`. |
| 4 | P1 | **Monthly cadence mis-detected** when a one-off shares the month with the regular bill (e.g. `Purchase awaiting refund` on the same day as `Clothing and household items`): the monthly extractor rejected the group and a 30-day *interval* series was fitted instead, shifting the projected bill dates. Affected request_77 and request_149 in the real dataset. | Monthly extraction keeps one member per calendar month (closest to the dominant day, then closest to the group's typical amount); month-end bills (28th–31st) are projected on the clamped last day. These two rows changed (request_77: `affordable_now` → `affordable_with_plan` with `stop:event_7143`; request_149: lower safe amount + change). Test: `test_month_end_and_leap_year_clamping`, planner tests. |
| 5 | P1 | `option_number` crashed on non-numeric option ids (`payment_option_1a`) → whole run failure. | Trailing-number parse with lexicographic fallback; found and covered by the property tests. |
| 6 | P1 | Malformed CSV values (bad amount/date, negative amounts, unknown user, non-positive request) crashed ingestion. | Tolerant event parsing with `Dataset.warnings`; unparseable/negative amounts become *unknown* (→ finding 1 semantics); malformed request rows are flagged and the CLI writes a conservative fallback row. Tests: `test_malformed_event_rows_…`, `test_malformed_request_row_…`. |
| 7 | P1 | Image evidence was trusted as-is: no currency/event-id/confidence/positivity validation of cache, VLM or OCR results (cache poisoning or a wrong-file hit would flow straight into the ledger). | `ImageExtractor.validate`: positive finite amount, currency matches the row, cache `event_id` matches, confidence ≥ 0.6; rejected evidence leaves the row unknown (finding 1). OCR candidates are only logged. Test: `test_image_cache_entries_are_validated_not_trusted`. |
| 8 | P1 | "Fixed amount" heuristic (`last two equal`) could freeze a variable expense series on a coincidental repeat. | Fixed = identical history or identical last three; the two-equal rule is kept for income only (salary level changes are step functions). |
| 9 | P1 | The 180-day phase replay for interval series was applied unconditionally (dataset regularity: every series starts `R-180+o`, o∈[2,6]). | Applied only when the series' history span is within `[shift−2·interval, shift]`; otherwise plain `last+interval`. Documented as a dataset-derived assumption. |
| 10 | P1 | The validator re-simulated plans with the engine's own `balance_path`/`plan_is_safe` (not independent). | Validator now walks the daily balance itself (`independent_walk`) and independently recomputes the change-free `amount_safe_to_pay` and `earliest_date_for_full_payment` from the forecast items, failing on any disagreement; blocked ledgers must produce the conservative row. All 250 rows agree. |
| 11 | P2 | FX quantisation used the implicit `ROUND_HALF_EVEN`. | Explicit `ROUND_HALF_UP`; rate direction is the stated `from→to` row, inverse pairs only as a logged fallback (none needed in this dataset). |
| 12 | P2 | Settled rows dated after the request date: credits were counted. | Future-dated "settled" credits are ignored until they are in the balance; debits are reserved. |

## Claims verified without change

- **Opening balance is not net of pending debits**: reserving pending debits reproduces samples 02, 20, 21, 22 within 0.2–1.5%; treating them as already deducted misses by 10%+.
- **FX**: rates are constant per pair in this dataset; projected foreign salaries use the row dated on their settlement date as the statement prescribes (`test_foreign_currency_converted_with_settlement_date_rate`); a historical row with only later rates uses the earliest later row with provenance (`earliest-later:`).
- **Monthly items within 90 days**: the ground truth projects monthly templates only for the request month and the two following calendar months (samples 05, 08, 13 — February rent excluded, earliest dates reproduced), while interval items run to `R+90` (sample 10 needs the early-March items). For the three requests dated on the 20th/25th/30th this extrapolates the same generator rule; `horizon_mode=days` is the conservative alternative and is tested (`test_four_monthly_occurrences_within_90_days_documented_behaviour`).
- **Same-day income/expense**: end-of-day netting; a payday full payment is funded by that day's income (samples 18, 19, 23).
- **Partial payment**: with `[R: safe, E: remainder]` no split can complete before the change-free earliest date and no first payment above `amount_safe_to_pay` is safe — 300 random splits checked (`test_partial_split_search_finds_no_better_plan_than_the_prescribed_one`).
- **Instalments**: schedule = `first + k·frequency`, amount reproduced exactly, `n ≤ max_installment_months`, last payment inside the window, total payable drives ranking.
- **Spending changes**: exhaustive over ≤3 changes on permitted, non-protected flexible series (stop/reduce mutually exclusive), smallest total reduction first; only generated when no change-free plan meets the deadline.
- **Determinism**: identical output under shuffled rows of every input file (`test_output_is_invariant_to_row_order_of_every_input_file`); no time-of-day dependence on the decision path (timestamps only in the usage report).
- **Model calls**: none on the decision path; VLM only behind `BUYORWAIT_ENABLE_VLM=1` + key, results validated as evidence.

## Verification (commands and results)

```
python -m pytest evaluation/tests -q          -> 67 passed
python -m ruff check code evaluation           -> All checks passed!
python code/main.py --usage evaluation/usage_run.json
                                               -> 250 rows, 0 model calls, 0 blocked, 0 failures, no dataset warnings
python evaluation/validate_output.py           -> VALID (independent walk + baseline recomputation agree on all 250)
python evaluation/evaluate_samples.py          -> status 96% / method 96% / plan 92% / earliest 100% / changes 96%;
                                                  amount_safe_to_pay within 5%: 88% (median rel. error 0.51%)
diff vs previous output.csv                    -> 2 rows changed (request_77, request_149; finding 4)
```

## Remaining risks / deferred

- Hidden base amounts of variable templates are unknowable; decisions within ~1% of the minimum can flip (documented).
- The calendar cut-off for monthly templates and the 180-day phase replay are dataset-derived generator rules; both are guarded/configurable but still extrapolations for the three late-month requests.
- Combining instalments with spending changes (23 rows) is a policy choice consistent with the statement's ranking rules; no solved sample exercises it.
- The validator's independence is arithmetic, not model-level: it reuses the engine's forecast items (a wrong template would be reproduced). A second, differently-structured forecaster would be the next step.
- Performance is not a concern (≈1 s for 250 requests); `explain()` recomputes the baseline path once per request (P2, left as is).
