# Token usage and cost report — final full-dataset run

- Run timestamp: 2026-09-12T20:59:01+05:30
- Dataset: `dataset` (250 requests in `requests.csv`)
- Output: `output.csv` (250 rows)
- Elapsed: 0.79 s (single process, deterministic)

## Model usage (LLM / VLM API calls)

| Provider / model | Calls | Input tokens | Output tokens | Total tokens | Est. cost (USD) |
|---|---:|---:|---:|---:|---:|
| none (no LLM/VLM calls were made) | 0 | 0 | 0 | 0 | 0.00 |
| **Overall** | **0** | **0** | **0** | **0** | **0.0000** |

## Per-request averages

- Model calls per request: 0.0000
- Tokens per request (input + output): 0.00
- Estimated cost per request: USD 0.000000
- Estimated total cost: USD 0.0000

## Zero-cost deterministic operations (not model calls)

- Ledger reconstruction, recurrence detection, 90-day forecast, candidate generation, ranking and CSV rendering run entirely in Python (`code/buyorwait/`) with exact decimal arithmetic. No network access.
- Image evidence: 16 image-backed blank amounts resolved from the verified extraction cache `code/cache/image_extractions.json` (sha256-keyed). The cache entries were produced during development by a vision-model review of each PNG with manual verification; the final run performed 0 vision-model calls.
- Message evidence: rule-based, bilingual (English/Indonesian) template parsing; 0 LLM calls.
- Explanations: template-rendered from the ledger facts; 0 LLM calls.

## Optional model paths (disabled in the final run)

- `BUYORWAIT_ENABLE_VLM=1` with `ANTHROPIC_API_KEY` set enables Claude vision extraction for images missing from the cache (model from `BUYORWAIT_VLM_MODEL`, default `claude-sonnet-5`); every call is metered into this report (prices from `BUYORWAIT_PRICE_IN_PER_MTOK` / `BUYORWAIT_PRICE_OUT_PER_MTOK`, defaults 3 / 15 USD per MTok).
- No API keys, credentials or sensitive configuration are stored in the repository.
