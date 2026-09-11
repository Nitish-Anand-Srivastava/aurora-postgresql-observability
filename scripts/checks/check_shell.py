#!/usr/bin/env python3
"""Lint shell scripts with shellcheck if available.

This repository ships no .sh scripts today (all automation is Python/PowerShell/Make), but this
check is included so any future shell script is linted automatically. It is a soft-dependency
check: if no .sh files exist, or shellcheck is not installed, it reports a warning (not a failure)
so `python scripts/validate.py` still passes on machines without shellcheck installed.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Result, tracked_files  # noqa: E402


def main() -> int:
    result = Result("Shell script lint (shellcheck)")
    files = tracked_files(["*.sh", "*.bash"])
    if not files:
        result.warn("no shell scripts (*.sh/*.bash) found in the repository - nothing to lint")
        result.print_report()
        return 0

    shellcheck = shutil.which("shellcheck")
    if not shellcheck:
        result.warn(
            "shellcheck is not installed; skipping shell lint for: "
            + ", ".join(str(f) for f in files)
        )
        result.print_report()
        return 0

    for f in sorted(files):
        proc = subprocess.run([shellcheck, str(f)], capture_output=True, text=True)
        if proc.returncode != 0:
            result.error(f"{f}:\n{proc.stdout}\n{proc.stderr}")

    result.print_report()
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
