"""Run the pipeline on dataset/sample_requests.csv and compare with the solved fields.

Usage (from repo root):
    python evaluation/evaluate_samples.py [--dataset dataset] [--estimator mean] [--verbose]

Reports per-field accuracy and lists every mismatch. No sample-specific logic
lives in the production code; this script only measures.
"""
from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "code"))

from buyorwait.config import EngineConfig  # noqa: E402
from buyorwait.ingest import load_dataset  # noqa: E402
from buyorwait.pipeline import Engine  # noqa: E402
from buyorwait.render import decision_row  # noqa: E402

FIELDS = [
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
]


def rel_err(a: str, b: str) -> float:
    try:
        x, y = Decimal(a), Decimal(b)
    except Exception:
        return 1.0
    if y == 0:
        return 0.0 if x == 0 else 1.0
    return float(abs(x - y) / abs(y))


def evaluate(cfg: EngineConfig, dataset_dir: str, verbose: bool = False, tol: float = 0.0) -> dict:
    ds = load_dataset(dataset_dir)
    engine = Engine(ds, cfg)
    hits = {f: 0 for f in FIELDS}
    within = {f: 0 for f in FIELDS}
    n = 0
    rows = []
    errs = []
    for req in ds.samples:
        exp = ds.sample_expectations[req.request_id]
        dec, ledger, an = engine.decide(req)
        got = decision_row(dec)
        n += 1
        line = {"request_id": req.request_id}
        for f in FIELDS:
            g, e = got[f], getattr(exp, f)
            ok = g == e
            if f == "amount_safe_to_pay":
                r = rel_err(g, e)
                errs.append(r)
                ok_tol = r <= tol
            else:
                ok_tol = ok
            hits[f] += int(ok)
            within[f] += int(ok_tol)
            line[f] = (g, e, ok)
        rows.append(line)
    report = {"n": n, "exact": {f: hits[f] / n for f in FIELDS}, "mean_rel_err_amount_safe": sum(errs) / max(1, len(errs)),
              "median_rel_err_amount_safe": sorted(errs)[len(errs) // 2] if errs else 0.0}
    if tol:
        report["within_tol"] = {f: within[f] / n for f in FIELDS}
    report["rows"] = rows
    return report


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", default=os.path.join(ROOT, "dataset"))
    p.add_argument("--estimator", default=None)
    p.add_argument("--tol", type=float, default=0.05, help="relative tolerance reported for amount_safe_to_pay")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)
    cfg = EngineConfig()
    if args.estimator:
        cfg.estimator = args.estimator
    rep = evaluate(cfg, args.dataset, args.verbose, tol=args.tol)
    print(f"samples: {rep['n']}")
    for f in FIELDS:
        extra = f"  (within {args.tol:.0%}: {rep['within_tol'][f]:.1%})" if f == "amount_safe_to_pay" and "within_tol" in rep else ""
        print(f"  {f:32s} exact={rep['exact'][f]:.1%}{extra}")
    print(f"  amount_safe_to_pay mean rel err={rep['mean_rel_err_amount_safe']:.3%} median={rep['median_rel_err_amount_safe']:.3%}")
    print("mismatches:")
    for line in rep["rows"]:
        bad = [(f, line[f][0], line[f][1]) for f in FIELDS if not line[f][2]]
        if bad:
            print(f"  {line['request_id']}:")
            for f, g, e in bad:
                print(f"      {f}: got={g!r} expected={e!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
