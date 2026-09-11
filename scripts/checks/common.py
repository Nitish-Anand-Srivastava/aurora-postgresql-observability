"""Shared helpers for scripts/checks/*.py.

Kept dependency-free (stdlib only) so every check can run with a bare Python 3.9+ interpreter.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def repo_root() -> Path:
    """Return the repository root (parent of the scripts/ directory this file lives under)."""
    return Path(__file__).resolve().parents[2]


def tracked_files(patterns: list[str] | None = None) -> list[Path]:
    """Return repo files (tracked + untracked-but-not-gitignored), preferring
    `git ls-files --cached --others --exclude-standard` (respects .gitignore and works before the
    first commit of new files) and falling back to a plain filesystem walk (skipping .git/ and
    common noise directories) if git is unavailable.
    """
    root = repo_root()
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            check=True,
            capture_output=True,
        ).stdout
        files = [root / p for p in out.decode("utf-8", "replace").split("\0") if p]
        if files:
            return _filter_patterns(files, patterns)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass

    skip_dirs = {".git", "node_modules", ".venv", "venv", "tools", "__pycache__"}
    files = []
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        if any(part in skip_dirs for part in p.parts):
            continue
        files.append(p)
    return _filter_patterns(files, patterns)


def _filter_patterns(files: list[Path], patterns: list[str] | None) -> list[Path]:
    if not patterns:
        return files
    root = repo_root()
    out = []
    for f in files:
        rel = f.relative_to(root).as_posix()
        if any(Path(rel).match(pat) for pat in patterns):
            out.append(f)
    return out


class Result:
    def __init__(self, name: str):
        self.name = name
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors

    def print_report(self) -> None:
        status = "PASS" if self.ok else "FAIL"
        print(f"[{status}] {self.name}", file=sys.stderr)
        for w in self.warnings:
            print(f"  warning: {w}", file=sys.stderr)
        for e in self.errors:
            print(f"  error: {e}", file=sys.stderr)
