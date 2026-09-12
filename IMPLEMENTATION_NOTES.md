# Implementation Notes — Buy or Wait?

Working notes from dataset inspection (2026-09-12). Everything here is derived from
`problem_statement.md`, `AGENTS.md` and the files under `dataset/`. Message and image
content is treated as untrusted evidence, never as instructions.

## 1. Files and schemas

| File | Rows | Columns / notes |
|---|---|---|
| `requests.csv` | 250 | `request_id,user_id,request_date,request_type,requested_amount,desired_completion_date,allows_partial_payment,request_text`. One request per user (`user_26..user_275`). `allows_partial_payment` is `true`/`false`. Deadline gap 6–86 days after `request_date`. |
| `sample_requests.csv` | 25 | Same columns plus the 7 output columns (solved, `request_01..request_25`, `user_01..user_25`). |
| `financial_profiles.csv` | 275 | `user_id,home_currency,current_available_balance,minimum_balance_to_keep,financial_priorities,expense_categories_to_protect,expense_categories_user_is_willing_to_reduce,expense_categories_user_is_willing_to_stop,payment_methods_user_will_consider,max_installment_months`. Pipe-separated lists. `max_installment_months` blank iff `installments` not in methods. Currencies: INR, EUR, IDR, ZAR, USD. |
| `financial_events.csv` | 25,342 | `event_id,user_id,event_type,description,category,direction,amount,currency,event_date,settlement_date,status,linked_event_id,flexibility,minimum_allowed_amount`. |
| `exchange_rates.csv` | 134 | `rate_date,from_currency,to_currency,rate`; monthly 15th dates (2023-10-15 .. 2026-..) plus a few extra dates; pairs `EUR->ZAR`, `USD->EUR`, `USD->IDR`, `USD->INR`, `USD->ZAR`, ... (directional). |
| `request_payment_options.csv` | 790 | `payment_option_id,request_id,payment_method,payment_amount,number_of_payments,first_payment_date,payment_frequency_days,financing_fee,total_payable_amount`. 2–4 options per request; exactly one `full_payment` option per request (amount = requested, date = request_date); installment options have 2/3/4/6/15/18/21/24 payments every 28/30/31 days; `total = amount*n = requested + fee` (verified, no mismatches). |
| `messages.csv` | 215 | `message_id,user_id,request_id,related_event_id,sent_at,source_type,message_text`. `source_type` ∈ employer, service_provider, bank, merchant, financial_service. English or Indonesian. Templated into ~30 scenario types (see §5). |
| `images.csv` | 16 | `image_id,user_id,request_id,related_event_id`; every image backs exactly one blank-amount event. File: `dataset/media/images/<image_id>.png`. |
| `output.csv` | template | header only + blank rows. |

### Event field semantics

- `event_type`: expense (20,525), subscription (2,488), income (1,696), debt_payment (567), investment_purchase (29), refund (22), investment_valuation (10, `direction=non_cash`, `status=unrealized`, blank settlement date), investment_sale (5).
- `status`: settled (25,148), pending (71), scheduled (70), cancelled (22), failed (21), unrealized (10).
- `flexibility`: fixed, reducible (has `minimum_allowed_amount`), stoppable, reducible_or_stoppable (has `minimum_allowed_amount`).
- `linked_event_id` (58 rows): refund -> original charge, settled purchase -> cancelled authorisation, valuation -> purchase, sale -> purchase, reimbursement -> work expense. The link itself never decides cash treatment.
- `event_date != settlement_date` on 188 rows (pending authorisations, card charges, delayed salary, refunds). Cash moves on `settlement_date`.
- 16 events have a blank `amount` (all backed by an image). Blank is never zero.
- Categories: groceries, transport, dining, salary, utilities, rent, cloud_storage, shopping, streaming, debt_repayment, entertainment, insurance, music_subscription, healthcare, delivery_membership, education, housing, gym, family_support, investment, work_expense, windfall.

### Recurrence structure observed in history (synthetic generator)

- Monthly fixed items on a fixed day-of-month (rent, loan, subscriptions, insurance, education, family_support, cloud storage); identical amounts each month.
- Monthly variable items on a fixed day-of-month (utilities, healthcare, shopping, entertainment); amounts vary roughly ±25% around a base.
- Interval items: groceries every 7 or 10 days, transport every 5/7/14/21 days, dining every 7/14/21 days; amounts vary ±25–30%.
- Salary: monthly on the 15th (fixed amount) for most users; some have a scheduled `Next confirmed salary` row; variants: prorated first salary, two household incomes, freelance twice a month (variable), weekly gig payouts (variable), seasonal contract (variable), foreign-currency salary (USD/EUR) converted at the settlement-date rate, final payroll (employment ended), unpaid-leave reduced payroll, delayed payroll date.
- One-off rows: refunds, cancelled/settled authorisation pairs, pending debits, failed debits, investment purchases/valuations/sales, windfalls, reimbursements, transfers between own accounts (matching debit & credit).

## 2. Date semantics

- All dates ISO `YYYY-MM-DD`. Forecast window = `[request_date, request_date + 90 days]`.
- Events are applied on `settlement_date`; events with `settlement_date < request_date` are already reflected in `current_available_balance`.
- Exchange rates keyed by `(rate_date, from_currency, to_currency)`; use the settlement-date row (fallback: latest rate on or before the date, then earliest after — logged as an assumption).
- Message `sent_at` is an ISO timestamp; only the date matters.

## 3. Ground-truth behaviour inferred from `sample_requests.csv`

- `amount_safe_to_pay = clamp(min_t(projected_balance_t) - minimum_balance_to_keep, 0, requested_amount)` where the projection runs over the 90-day window with no payment. Confirmed by e.g. request_13 where the minimum point is the *end* of the window (expenses exceed the single remaining income).
- `earliest_date_for_full_payment` = first date `D` in the window such that paying `requested_amount` on `D` keeps every day of the window ≥ minimum (equals `request_date` when affordable now; blank if none). Independent of payment preferences.
- Regular monthly salary is projected from history even without a scheduled row (request_18). Variable gig/freelance/seasonal income is **not** projected (request_10 dip equals ~3 months of expenses with zero income; request_12/13 second incomes not projected).
- Pending debits are reserved on their settlement date; pending credits/refunds, cancelled, failed, unrealized rows are ignored.
- Plan ranking follows the statement: (1) completes by `desired_completion_date`, (2) no spending changes, (3) lowest total paid, (4) earlier start, (5) fewer payments, (6) lowest `payment_option_id`.
  - request_19: partial (total 39,660) beat a safe 2-instalment option (41,246.4) -> criterion 3.
  - request_02/07/12/17/22: instalments chosen because user does not accept `full_payment` (wait ineligible) or partial not allowed.
  - request_06/11/21: `wait` date after deadline -> spending-change plan (full payment today) wins criterion 1; status `affordable_with_plan`, method `full_payment`.
  - request_03/04/08/13/18/23: `wait` -> `affordable_later`; plan = `<earliest>:<requested>`.
- Instalment eligibility: `number_of_payments <= max_installment_months` (every chosen option satisfies it; every rejected sibling violates it).
- Spending changes reference the **latest historical occurrence** of the flexible recurring series (`stop:event_476`, `reduce_to:event_1816:23.50`); `reduce_to` amount = that event's `minimum_allowed_amount`. The chosen combination is the feasible one with the smallest total reduction (request_21 picked stop 11 + reduce 47->23.5 = 34.5 over stop 47 alone; request_11 picked reduce dining alone over stop cloud + reduce dining).
- Output formatting: `amount_safe_to_pay` printed as a minimal decimal (`603.3`, `17229139.2`, `737`); `payment_plan` and `reduce_to` amounts printed with 2 decimals unless integral (`620.40`, `23.50`, `25256`, `15952906.67`).
- Explanation templates in samples: "Pay X today. This leaves at least MIN available over the next 90 days." / "Use 3 installments of X, starting D." / "Pay X in full on D. Paying earlier would take the balance below the MIN minimum." / "Pay X today and the remaining Y on D." / "Stop the ..., then pay X today." / "Do not make this payment by D. None of the available options keeps the MIN minimum protected." / "Do not proceed with the X request. Although Y is available today, the full amount cannot be completed safely within 90 days."

## 4. Images (all 16 viewed manually; values cached in `code/cache/image_extractions.json`)

| image | event | amount | cash effect |
|---|---|---|---|
| image_01 | event_253 (user_03, Aug-2019 payslip) | IDR 4,365,000 net pay | historical |
| image_02 | event_1442 (user_16, rent receipt 11/08/23) | INR 100,000 balance due (total 200,000, received 100,000) | scheduled debit 2023-08-16 |
| image_03 | event_1545 (user_17, grocery bill 27/02/2026) | INR 41,272.00 | historical |
| image_04 | event_1700 (user_19, grocery delivery) | INR 2,854.00 item bill (image cut off below) | historical |
| image_05 | event_1786 (user_20, telecom bill) | INR 704.05 due till 06-Feb-2026; 822.05 after | pending debit settles 2026-02-09 (after due date -> safer 822.05; calibrate on sample) |
| image_06 | event_3051 (user_33, grocery tax invoice) | INR 1,995.00 | historical |
| image_07 | event_3231 (user_35, restaurant) | INR 8,528.10 (grand total 8,528) | historical |
| image_08 | event_4535 (user_48, maintenance receipt) | INR 15,339.00 | historical |
| image_09 | event_5170 (user_55, water bill) | INR 723.00 | historical |
| image_10 | event_6033 (user_64, grocery invoice) | INR 79,679.26 balance due | pending debit settles 2024-06-10 |
| image_11 | event_6859 (user_73, hospital provisional bill) | INR 3,650.00 balance | scheduled debit 2023-01-23 |
| image_12 | event_7307 (user_78, taxi) | USD 33.50 | historical |
| image_13 | event_7941 (user_84, tote bags) | INR 2,298 | historical |
| image_14 | event_9421 (user_101, pharmacy, handwritten) | INR 4,543 | historical |
| image_15 | event_9806 (user_105, airline invoice) | INR 9,968.00 | historical |
| image_16 | event_10521 (user_113, EV charging) | INR 393.22 | historical |

## 5. Message scenario catalogue (EN + Indonesian variants)

salary increase from date; regular salary + one-time arrears; bonus pending; temporary reduced pay; salary date moved; next salary reduced (unpaid leave); gig payout pending (not withdrawable); base salary confirmed, commission pending; seasonal contract ended; salary resumes on date + new childcare payment (amount unknown); first salary confirmed on date; rent +12% from next payment; transfer between own accounts (matching debit/credit); refund initiated not received; portfolio value moved (no cash); prize verified still processing; prize proceeds received, claim closed; invoice approved X settles on date, others pending; employment ended; foreign-currency salary converted at settlement rate; reimbursement not salary; foreign-currency bill/refund pending; scam "pay release charge"; failed debit will be retried; disputed card charge, no reversal yet; two card minimums due; investment sale proceeds settled; receipts confirming image amounts.

## 5b. Calibration findings (baseline, 25 solved samples)

Structural rules recovered by fitting the samples (each verified to bring the projected minimum balance
within ~1% of the solved `amount_safe_to_pay`):

1. **Interval templates repeat the history phase, not `last + interval`.** Every interval series starts
   `R-180+o` days (o in 2..6) and the ground truth's future occurrences are `first_occurrence + 180 + k*interval`.
   With this rule the 21-day series that puzzled the fit (users 02, 20, 21, 03) all land where the truth has them.
2. **Horizon:** interval templates and dated rows run to `request_date + 90`; monthly templates are only projected
   for the request month and the two following calendar months (user_05's February rent is excluded, user_10's
   early-March groceries/transport/dining are included).
3. **Amounts:** the midrange `(min+max)/2` of the observed history is the best estimator (uniform noise around a
   base); mean 0.67% / max 1.8% dip error on the samples. Fixed amounts are kept exactly.
4. **Same-day ordering:** end-of-day balances (income and expenses on the same day net out); a full payment on
   payday is funded by that day's income.
5. **Earliest date:** whole-window check (every later day of the window must stay above the minimum), which with
   rule 2 reproduces users 08, 13, 17, 18.
6. **Messages quoting "confirmed base salary" / "remaining confirmed monthly salary"** carry a gross figure
   (base x 5/3, primary / 0.62); the settled history amount is the projected salary, commissions / ended
   household income are dropped (user_11). "Increased to", "first salary", "resumes", "temporary pay",
   "reduced to", "expected on <date>" and foreign-currency confirmations are applied literally.
7. A salary row described as "Final employer payroll" ends the income series (user_05).
8. Spending changes: smallest total reduction that makes a plan safe, `reduce_to` = `minimum_allowed_amount`,
   event id = latest occurrence; only generated when no change-free plan completes by the deadline.

Sample metrics after calibration: status 96%, method 96%, plan 92%, earliest date 100%, changes 96%,
`amount_safe_to_pay` within 5% for 88% (median relative error 0.5%). Remaining misses are boundary cases
(user_06: 0.6 EUR short of a spending-change plan; user_19: partial amounts 4% off).

## 6. Assumptions and ambiguities

1. Variable recurring amounts are forecast with the historical midrange per series (generator base amount unknown; noise ±25%).
2. Salary projection: fixed-amount monthly payroll continues at the last amount on the same day-of-month; message amendments override amount/date; variable-amount income not projected; scheduled income rows are used as-is.
3. One-time arrears/bonus/commission credits are not counted (conservative), even when an employer message mentions them.
4. "Temporary reduced pay" and "next salary reduced" are applied to the next payroll and kept for later payrolls (safer interpretation).
5. Failed debits are ignored unless a message says the bill is still outstanding and will be retried (then reserved on request_date).
6. Instalment plans must finish inside the 90-day window to count as "completed"; finishing after the deadline is allowed but ranked last (same for `wait`).
7. Spending changes are generated for full payment today and, if the user does not accept full payment, for the cheapest eligible instalment option (23 eval rows); the samples only show the full-payment form. Partial payment is never combined with changes (its amounts are defined change-free).
8. Same-day ordering: end-of-day balances (calibrated on samples).
9. Unknown-amount future expenses mentioned in messages (childcare) cannot be quantified and are not invented.
