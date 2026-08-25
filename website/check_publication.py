#!/usr/bin/env python3
"""Prevent private reference material from entering public documentation builds."""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_ROOT_FILES = (
    "README.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "SECURITY.md",
)
PRIVATE_REFERENCE = re.compile(
    r"HARBORRAG_ARCHITECTURE\.md|harborrag-architecture\.html",
    re.IGNORECASE,
)
GENERATED_TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".txt", ".xml"}


def _matches_in_file(path: Path, root: Path) -> list[str]:
    failures: list[str] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = PRIVATE_REFERENCE.search(line)
        if match:
            failures.append(f"{path.relative_to(root)}:{line_number}: {match.group(0)}")
    return failures


def find_public_reference_leaks(root: Path = REPOSITORY_ROOT) -> list[str]:
    """Find public source pages that reference a private input path or filename."""
    candidates = [root / name for name in PUBLIC_ROOT_FILES]
    docs_root = root / "docs"
    if docs_root.exists():
        candidates.extend(docs_root.rglob("*.md"))
    candidates.extend((root / "website" / "templates").rglob("*.html"))
    candidates.append(root / "website" / "README.md")

    failures: list[str] = []
    for path in sorted(set(candidates)):
        if path.is_file():
            failures.extend(_matches_in_file(path, root))
    return failures


def find_generated_reference_leaks(
    root: Path = REPOSITORY_ROOT, site_directory: str = "site"
) -> list[str]:
    """Find private input references accidentally copied into a generated site."""
    site_root = root / site_directory
    if not site_root.exists():
        return []

    failures: list[str] = []
    for path in sorted(site_root.rglob("*")):
        if path.is_file() and path.suffix.lower() in GENERATED_TEXT_SUFFIXES:
            failures.extend(_matches_in_file(path, root))
    return failures


def publication_failures(root: Path = REPOSITORY_ROOT) -> list[str]:
    """Collect all public-publication guard failures."""
    return [
        *find_public_reference_leaks(root),
        *find_generated_reference_leaks(root),
    ]


def main() -> int:
    """Run the public documentation boundary checks."""
    failures = publication_failures()
    if failures:
        print("Public documentation boundary violations found:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Public documentation contains no private reference material.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
