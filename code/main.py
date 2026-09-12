"""Buy or Wait? — command-line entry point.

Usage (from the repository root):
    python code/main.py                       # dataset/ -> output.csv
    python code/main.py --requests dataset/sample_requests.csv --output out_samples.csv
    python code/main.py --trace traces/       # also dump one JSON trace per request
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from buyorwait.config import EngineConfig  # noqa: E402
from buyorwait.ingest import load_dataset  # noqa: E402
from buyorwait.pipeline import Engine  # noqa: E402
from buyorwait.render import write_output  # noqa: E402


def parse_args(argv=None):
    root = os.path.dirname(HERE)
    p = argparse.ArgumentParser(description="Buy or Wait? deterministic financial decision agent")
    p.add_argument("--dataset", default=os.path.join(root, "dataset"), help="dataset directory (default: <repo>/dataset)")
    p.add_argument("--requests", default=None, help="requests CSV to evaluate (default: <dataset>/requests.csv)")
    p.add_argument("--output", default=os.path.join(root, "output.csv"), help="output CSV path (default: <repo>/output.csv)")
    p.add_argument("--trace", default=None, help="directory to write per-request JSON traces")
    p.add_argument("--usage", default=None, help="write a JSON usage summary (model calls/tokens) to this path")
    p.add_argument("--limit", type=int, default=None, help="only process the first N requests")
    p.add_argument("--estimator", default=None, help="override variable-amount estimator (mean|median|last|max|mean3|midrange|trimmed_mean)")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    t0 = time.time()
    ds = load_dataset(args.dataset)
    if args.requests:
        from buyorwait.ingest import _read  # local helper
        # reuse the request parser through a temporary dataset view
        import csv
        from buyorwait.ingest import Request, parse_bool, parse_date, parse_decimal

        with open(args.requests, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        requests = [
            Request(
                request_id=r["request_id"], user_id=r["user_id"], request_date=parse_date(r["request_date"]),
                request_type=r.get("request_type", ""), requested_amount=parse_decimal(r["requested_amount"]),
                desired_completion_date=parse_date(r["desired_completion_date"]),
                allows_partial_payment=parse_bool(r.get("allows_partial_payment", "")), request_text=r.get("request_text", ""),
            )
            for r in rows
        ]
    else:
        requests = ds.requests
    if args.limit:
        requests = requests[: args.limit]
    cfg = EngineConfig()
    if args.estimator:
        cfg.estimator = args.estimator
    engine = Engine(ds, cfg)
    decisions = []
    if args.trace:
        os.makedirs(args.trace, exist_ok=True)
    for i, req in enumerate(requests):
        dec, ledger, an = engine.decide(req, with_trace=bool(args.trace))
        decisions.append(dec)
        if args.trace:
            with open(os.path.join(args.trace, f"{req.request_id}.json"), "w", encoding="utf-8") as f:
                json.dump(dec.trace, f, indent=2, default=str)
        if not args.quiet and (i + 1) % 50 == 0:
            print(f"  processed {i + 1}/{len(requests)}", file=sys.stderr)
    write_output(args.output, decisions)
    usage = engine.meter.summary()
    usage["requests"] = len(requests)
    usage["image_extraction_warnings"] = list(engine.extractor.log)
    usage["elapsed_seconds"] = round(time.time() - t0, 2)
    from datetime import datetime, timezone
    usage["timestamp"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    usage["dataset"] = args.dataset
    usage["requests_file"] = args.requests or os.path.join(args.dataset, "requests.csv")
    if args.usage:
        with open(args.usage, "w", encoding="utf-8") as f:
            json.dump(usage, f, indent=2)
    if not args.quiet:
        print(f"wrote {len(decisions)} rows to {args.output} in {usage['elapsed_seconds']}s; model calls: {usage['total']['calls']}")
        for line in engine.extractor.log:
            print("  image:", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
