#!/usr/bin/env python3
"""Validate every tracked JSON file parses correctly (stdlib json only)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Result, tracked_files  # noqa: E402


def main() -> int:
    result = Result("JSON syntax")
    files = tracked_files(["*.json"])
    if not files:
        result.warn("no JSON files found")
    for f in sorted(files):
        try:
            json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            result.error(f"{f}: {exc}")
        except UnicodeDecodeError as exc:
            result.error(f"{f}: {exc}")
    result.print_report()
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
