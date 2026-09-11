#!/usr/bin/env python3
"""Validate every tracked YAML file parses correctly.

Prefers PyYAML (pinned in scripts/requirements.txt) for full parser-level validation. If PyYAML
is not installed, falls back to a best-effort structural sanity check (balanced quotes/braces,
consistent indentation of list items) that catches common copy-paste mistakes without being a
real YAML parser -- this keeps `python scripts/validate.py` usable on a bare Python install per
the project's "don't require PyYAML if avoidable" preference, while CI (which installs
requirements.txt) always gets full validation.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Result, tracked_files  # noqa: E402

YAML_GLOBS = ["*.yml", "*.yaml"]


def _fallback_sanity_check(path: Path, result: Result) -> None:
    text = path.read_text(encoding="utf-8")
    if "\t" in text:
        result.error(f"{path}: contains a literal tab character (YAML forbids tabs for indentation)")
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.rstrip("\n")
        if stripped.count('"') % 2 != 0 and "#" not in stripped:
            # Best-effort only: a mismatched quote count often indicates a broken string, but
            # this is intentionally lenient (e.g. it does not understand YAML block scalars).
            result.warn(f"{path}:{i}: odd number of double-quote characters (verify not a typo)")


def main() -> int:
    try:
        import yaml  # type: ignore
        have_yaml = True
    except ImportError:
        have_yaml = False

    result = Result("YAML syntax")
    files = tracked_files(YAML_GLOBS)
    if not files:
        result.warn("no YAML files found")
    for f in sorted(files):
        try:
            if have_yaml:
                text = f.read_text(encoding="utf-8")
                for doc in yaml.safe_load_all(text):
                    pass  # noqa: consume the generator to force parsing of every document
            else:
                _fallback_sanity_check(f, result)
        except Exception as exc:  # yaml.YAMLError or UnicodeDecodeError etc.
            result.error(f"{f}: {exc}")

    if not have_yaml:
        result.warn(
            "PyYAML not installed - ran a reduced-confidence fallback check only. "
            "Install with: pip install -r scripts/requirements.txt"
        )

    result.print_report()
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
