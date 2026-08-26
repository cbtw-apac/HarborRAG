#!/usr/bin/env python3
"""Reject stale predecessor branding in active website sources."""

from __future__ import annotations

import re
from pathlib import Path

WEBSITE_ROOT = Path(__file__).resolve().parent
SOURCE_SUFFIXES = {".html", ".json", ".md", ".py", ".txt"}
FORBIDDEN = re.compile(
    r"QDrant Loader|Qdrant Loader|qdrant-loader|martin-papy|github\.com/harborrag/harborrag",
    re.IGNORECASE,
)


def find_stale_branding() -> list[str]:
    """Return source locations containing stale branding without an explicit exemption."""
    failures: list[str] = []
    for path in sorted(WEBSITE_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES or path == Path(__file__):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, 1):
            previous_line = lines[line_number - 2] if line_number > 1 else ""
            if "branding-compat" in line or "branding-compat" in previous_line:
                continue
            match = FORBIDDEN.search(line)
            if match:
                relative_path = path.relative_to(WEBSITE_ROOT.parent)
                failures.append(f"{relative_path}:{line_number}: {match.group(0)}")
    return failures


def main() -> int:
    """Run the branding drift check."""
    failures = find_stale_branding()
    if failures:
        print("Stale website branding found:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Website branding is synchronized with HarborRAG.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
