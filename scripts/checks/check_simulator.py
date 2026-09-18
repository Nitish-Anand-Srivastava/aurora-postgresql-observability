#!/usr/bin/env python3
"""Run database-free tests for the non-production tuning simulator."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Result, repo_root  # noqa: E402


def main() -> int:
    result = Result("Aurora tuning simulator")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_aurora_tuning_simulator.py",
        ],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        result.error((completed.stdout + completed.stderr).strip())
    result.print_report()
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
