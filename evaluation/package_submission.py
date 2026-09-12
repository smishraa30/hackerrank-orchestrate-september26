"""Build code.zip for submission (code + evaluation + docs; no dataset, media, caches or secrets).

Usage (from repo root):
    python evaluation/package_submission.py [--output code.zip] [--list]
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INCLUDE = [
    ("code/README.md", "README.md"),
    ("code/README.md", "code/README.md"),
    ("IMPLEMENTATION_NOTES.md", "IMPLEMENTATION_NOTES.md"),
    ("requirements.txt", "requirements.txt"),
    ("code/main.py", "code/main.py"),
    ("code/cache/image_extractions.json", "code/cache/image_extractions.json"),
    ("evaluation/validate_output.py", "evaluation/validate_output.py"),
    ("evaluation/evaluate_samples.py", "evaluation/evaluate_samples.py"),
    ("evaluation/write_usage_report.py", "evaluation/write_usage_report.py"),
    ("evaluation/package_submission.py", "evaluation/package_submission.py"),
    ("evaluation/usage_report.md", "evaluation/usage_report.md"),
    ("evaluation/tests/conftest.py", "evaluation/tests/conftest.py"),
    ("evaluation/tests/test_engine.py", "evaluation/tests/test_engine.py"),
    ("evaluation/tests/test_messages.py", "evaluation/tests/test_messages.py"),
    ("evaluation/tests/test_recurrence.py", "evaluation/tests/test_recurrence.py"),
    ("evaluation/tests/test_planner.py", "evaluation/tests/test_planner.py"),
    ("evaluation/tests/test_validator_and_properties.py", "evaluation/tests/test_validator_and_properties.py"),
    ("ruff.toml", "ruff.toml"),
    ("ROADMAP.md", "ROADMAP.md"),
    ("ARCHITECTURE.md", "ARCHITECTURE.md"),
    ("AUDIT_REPORT.md", "AUDIT_REPORT.md"),
    ("VERIFICATION.md", "VERIFICATION.md"),
    ("PROMPTS.md", "PROMPTS.md"),
    ("DEVELOPMENT_SUMMARY.md", "DEVELOPMENT_SUMMARY.md"),
    ("evaluation/tests/test_audit.py", "evaluation/tests/test_audit.py"),
]
EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", "dataset", "media", "traces", ".git"}
# key-shaped strings that must never appear in packaged files (assembled so this file does not match itself)
SECRET_MARKERS = tuple("".join(parts) for parts in (("sk-", "ant-api"), ("ANTHROPIC_API_KEY", "=sk"), ("OPENAI_API_KEY", "=sk")))


def collect() -> list[tuple[str, str]]:
    files = list(INCLUDE)
    pkg = os.path.join(ROOT, "code", "buyorwait")
    for name in sorted(os.listdir(pkg)):
        if name.endswith(".py"):
            files.append((f"code/buyorwait/{name}", f"code/buyorwait/{name}"))
    out = []
    for src, dst in files:
        path = os.path.join(ROOT, src)
        if not os.path.exists(path):
            print(f"warning: missing {src}", file=sys.stderr)
            continue
        if any(part in EXCLUDED_DIRS for part in src.split("/")):
            continue
        out.append((path, dst))
    return out


def scan_secrets(path: str) -> None:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return
    for marker in SECRET_MARKERS:
        if marker in text:
            raise SystemExit(f"refusing to package {path}: contains a key-shaped string")


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--output", default=os.path.join(ROOT, "code.zip"))
    p.add_argument("--list", action="store_true")
    args = p.parse_args(argv)
    files = collect()
    if args.list:
        for _, dst in files:
            print(dst)
        return 0
    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as z:
        for src, dst in files:
            scan_secrets(src)
            z.write(src, dst)
    size = os.path.getsize(args.output)
    print(f"wrote {args.output}: {len(files)} files, {size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
