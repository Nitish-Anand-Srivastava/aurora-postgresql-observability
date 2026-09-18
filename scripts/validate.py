#!/usr/bin/env python3
"""Cross-platform validation entrypoint for aurora-postgresql-observability.

Runs every check in scripts/checks/ and prints a final summary. Exits non-zero if any check
reports an error (warnings do not fail the run).

Usage:
    python scripts/validate.py            # run everything
    python scripts/validate.py --only yaml json     # run a subset (see --list)
    python scripts/validate.py --list     # show available check names

Works with a bare Python 3.9+ standard library install; installing scripts/requirements.txt
(PyYAML) upgrades the YAML check from a best-effort fallback to full parser validation. The
Prometheus check downloads a pinned `promtool` binary into ./tools/ (gitignored) on first run and
requires network access to github.com for that one-time download; every other check is fully
offline.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "checks"))

CHECKS = {
    "yaml": "check_yaml",
    "json": "check_json",
    "dashboard": "check_dashboard_json",
    "prometheus": "check_prometheus",
    "shell": "check_shell",
    "markdown": "check_markdown_links",
    "postgres": "check_postgres",
    "simulator": "check_simulator",
    "secrets": "check_secrets",
    "setup": "check_setup",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="+", choices=sorted(CHECKS), help="run only these checks")
    parser.add_argument("--list", action="store_true", help="list available checks and exit")
    args = parser.parse_args()

    if args.list:
        for name in sorted(CHECKS):
            print(name)
        return 0

    selected = args.only or sorted(CHECKS)
    overall_ok = True
    print(f"Running {len(selected)} check(s): {', '.join(selected)}\n", file=sys.stderr)

    for name in selected:
        module = importlib.import_module(CHECKS[name])
        print(f"--- {name} ---", file=sys.stderr)
        rc = module.main()
        overall_ok = overall_ok and (rc == 0)
        print("", file=sys.stderr)

    if overall_ok:
        print("All validation checks passed.", file=sys.stderr)
    else:
        print("One or more validation checks FAILED. See errors above.", file=sys.stderr)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
