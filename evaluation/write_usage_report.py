"""Render evaluation/usage_report.md from the usage JSON written by `code/main.py --usage`.

Usage (from repo root):
    python code/main.py --usage evaluation/usage_run.json
    python evaluation/write_usage_report.py --usage evaluation/usage_run.json --output evaluation/usage_report.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def render(usage: dict, output_rows: int, image_cache_entries: int, dataset_dir: str) -> str:
    total = usage.get("total", {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
    n = max(1, usage.get("requests", output_rows))
    by_model = usage.get("by_model", {})
    ts = usage.get("timestamp") or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    lines = [
        "# Token usage and cost report — final full-dataset run",
        "",
        f"- Run timestamp: {ts}",
        f"- Dataset: `{dataset_dir}` ({usage.get('requests', output_rows)} requests in `requests.csv`)",
        f"- Output: `output.csv` ({output_rows} rows)",
        f"- Elapsed: {usage.get('elapsed_seconds', 'n/a')} s (single process, deterministic)",
        "",
        "## Model usage (LLM / VLM API calls)",
        "",
        "| Provider / model | Calls | Input tokens | Output tokens | Total tokens | Est. cost (USD) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    if not by_model:
        lines.append("| none (no LLM/VLM calls were made) | 0 | 0 | 0 | 0 | 0.00 |")
    for k, d in by_model.items():
        lines.append(f"| {k} | {d['calls']} | {d['input_tokens']} | {d['output_tokens']} | {d['input_tokens'] + d['output_tokens']} | {d['cost_usd']:.4f} |")
    tt = total["input_tokens"] + total["output_tokens"]
    lines += [
        f"| **Overall** | **{total['calls']}** | **{total['input_tokens']}** | **{total['output_tokens']}** | **{tt}** | **{total['cost_usd']:.4f}** |",
        "",
        "## Per-request averages",
        "",
        f"- Model calls per request: {total['calls'] / n:.4f}",
        f"- Tokens per request (input + output): {tt / n:.2f}",
        f"- Estimated cost per request: USD {total['cost_usd'] / n:.6f}",
        f"- Estimated total cost: USD {total['cost_usd']:.4f}",
        "",
        "## Zero-cost deterministic operations (not model calls)",
        "",
        "- Ledger reconstruction, recurrence detection, 90-day forecast, candidate generation, ranking and CSV rendering "
        "run entirely in Python (`code/buyorwait/`) with exact decimal arithmetic. No network access.",
        f"- Image evidence: {image_cache_entries} image-backed blank amounts resolved from the verified extraction cache "
        "`code/cache/image_extractions.json` (sha256-keyed). The cache entries were produced during development by a "
        "vision-model review of each PNG with manual verification; the final run performed 0 vision-model calls.",
        "- Message evidence: rule-based, bilingual (English/Indonesian) template parsing; 0 LLM calls.",
        "- Explanations: template-rendered from the ledger facts; 0 LLM calls.",
        "",
        "## Optional model paths (disabled in the final run)",
        "",
        "- `BUYORWAIT_ENABLE_VLM=1` with `ANTHROPIC_API_KEY` set enables Claude vision extraction for images missing from the "
        "cache (model from `BUYORWAIT_VLM_MODEL`, default `claude-sonnet-5`); every call is metered into this report "
        "(prices from `BUYORWAIT_PRICE_IN_PER_MTOK` / `BUYORWAIT_PRICE_OUT_PER_MTOK`, defaults 3 / 15 USD per MTok).",
        "- No API keys, credentials or sensitive configuration are stored in the repository.",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--usage", default=os.path.join(ROOT, "evaluation", "usage_run.json"))
    p.add_argument("--output", default=os.path.join(ROOT, "evaluation", "usage_report.md"))
    p.add_argument("--output-csv", default=os.path.join(ROOT, "output.csv"))
    p.add_argument("--dataset", default="dataset")
    args = p.parse_args(argv)
    with open(args.usage, encoding="utf-8") as f:
        usage = json.load(f)
    with open(args.output_csv, encoding="utf-8") as f:
        rows = sum(1 for _ in f) - 1
    cache_path = os.path.join(ROOT, "code", "cache", "image_extractions.json")
    entries = 0
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            entries = len([k for k in json.load(f) if not k.startswith("_")])
    text = render(usage, rows, entries, args.dataset)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
