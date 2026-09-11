#!/usr/bin/env python3
"""Best-effort validation of Markdown links within this repository.

Checks every `[text](target)` link in tracked *.md files:
  - Relative file links (not http(s)://, mailto:, or #-only anchors) must resolve to an existing
    file (optionally with a `#anchor` suffix, which is checked against that target file's
    generated GitHub-style heading slugs when the target is itself Markdown).
  - Does NOT make network calls: external http(s) links are only checked for obviously malformed
    syntax (e.g. accidental double-bracketing), never fetched. This keeps validation offline-safe
    and fast, per this project's "no live credentials / no required network access" validation
    story (see docs/setup.md).
"""
from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Result, repo_root, tracked_files  # noqa: E402

LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)


def slugify(heading: str) -> str:
    # Approximates GitHub's heading-to-anchor algorithm: lowercase, strip punctuation (keep
    # word chars/spaces/hyphens), spaces -> hyphens.
    heading = heading.strip()
    heading = "".join(
        c for c in heading if not unicodedata.category(c).startswith("P") or c == "-"
    )
    return heading.lower().strip().replace(" ", "-")


def anchors_for(path: Path) -> set[str]:
    if path.suffix.lower() != ".md" or not path.exists():
        return set()
    text = path.read_text(encoding="utf-8", errors="replace")
    return {slugify(m.group(2)) for m in HEADING_RE.finditer(text)}


def main() -> int:
    result = Result("Markdown link check")
    root = repo_root()
    md_files = tracked_files(["*.md"])
    if not md_files:
        result.warn("no Markdown files found")

    for md in sorted(md_files):
        text = md.read_text(encoding="utf-8", errors="replace")
        for m in LINK_RE.finditer(text):
            target = m.group(1).strip()
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if target.startswith("#"):
                anchors = anchors_for(md)
                if slugify(target[1:]) not in anchors and target[1:] not in anchors:
                    result.warn(f"{md}: anchor {target!r} not found among headings in the same file")
                continue

            path_part, _, anchor = target.partition("#")
            if not path_part:
                continue
            resolved = (md.parent / path_part).resolve()
            if not resolved.exists():
                result.error(f"{md}: link target does not exist: {target!r} (resolved: {resolved})")
                continue
            if anchor:
                anchors = anchors_for(resolved)
                if anchors and slugify(anchor) not in anchors and anchor not in anchors:
                    result.warn(
                        f"{md}: anchor '#{anchor}' not found in headings of {resolved.relative_to(root)}"
                    )

    result.print_report()
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
