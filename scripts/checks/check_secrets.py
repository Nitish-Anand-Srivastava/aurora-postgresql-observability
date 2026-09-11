#!/usr/bin/env python3
"""Scan tracked files for patterns that look like committed secrets/credentials.

This is a heuristic, defense-in-depth scan -- not a replacement for git-secrets/gitleaks/etc. It
deliberately targets high-confidence patterns (AWS key ID formats, private key headers) plus a
generic "password/secret assignment" heuristic that explicitly allow-lists this repo's own
placeholder conventions (REPLACE_ME, changeme, *.invalid hostnames, etc.) so intentional example
content in *.example/*.template files does not trip it, while a real accidental credential still
would.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Result, repo_root, tracked_files  # noqa: E402

# Files that intentionally contain literal example/placeholder credentials.
ALLOWED_SUFFIXES = (".example", ".template")
ALLOWED_NAME_HINTS = ("readonly-policy", "trust-policy-example")

PLACEHOLDER_HINTS = (
    "replace",
    "change",
    "example",
    "placeholder",
    "xxxxx",
    "your-",
    "todo",
    "invalid",
    "test",
    "sample",
    "dummy",
    "fake",
)

HIGH_CONFIDENCE_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key ID"),
    (re.compile(r"ASIA[0-9A-Z]{16}"), "AWS temporary (STS) access key ID"),
    (re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH |)PRIVATE KEY-----"), "PEM private key block"),
    (re.compile(r"(?i)aws_secret_access_key\s*=\s*[A-Za-z0-9/+=]{30,}"), "AWS secret access key literal"),
]

GENERIC_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token)\b\s*[:=]\s*['\"]?([^'\"\s]{6,})"
)


def is_exempt_file(rel_path: str) -> bool:
    if rel_path.endswith(ALLOWED_SUFFIXES):
        return True
    if any(hint in rel_path for hint in ALLOWED_NAME_HINTS):
        return True
    if rel_path.startswith("scripts/checks/check_secrets.py"):
        return True
    if rel_path in ("SECURITY.md", "CONTRIBUTING.md", ".gitignore"):
        return True
    return False


def looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(hint in lowered for hint in PLACEHOLDER_HINTS)


def main() -> int:
    result = Result("Secret pattern scan")
    files = tracked_files()
    binary_exts = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".gz", ".tar"}
    root = repo_root()

    for f in sorted(files):
        rel = f.relative_to(root).as_posix()
        if f.suffix.lower() in binary_exts:
            continue
        if is_exempt_file(rel):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern, label in HIGH_CONFIDENCE_PATTERNS:
                if pattern.search(line):
                    result.error(f"{rel}:{lineno}: looks like a {label}")

            m = GENERIC_ASSIGNMENT.search(line)
            if m and not looks_like_placeholder(line) and "#" not in line.split(m.group(0))[0]:
                result.error(
                    f"{rel}:{lineno}: possible hardcoded credential (matched {m.group(1)!r}); "
                    "if this is an intentional placeholder, name the file *.example/*.template or "
                    "include an obvious placeholder marker (REPLACE_ME, changeme, etc.)"
                )

    result.print_report()
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
