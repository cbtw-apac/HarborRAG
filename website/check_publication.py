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
            failures.append(f"{path.relative_to(root).as_posix()}:{line_number}: {match.group(0)}")
    return failures


def public_candidate_files(root: Path = REPOSITORY_ROOT) -> list[Path]:
    """Return the public source files the guard is expected to examine."""
    candidates = [root / name for name in PUBLIC_ROOT_FILES]
    docs_root = root / "docs"
    if docs_root.exists():
        candidates.extend(docs_root.rglob("*.md"))
    candidates.extend((root / "website" / "templates").rglob("*.html"))
    candidates.append(root / "website" / "README.md")
    return [path for path in sorted(set(candidates)) if path.is_file()]


def find_guard_scope_failures(root: Path = REPOSITORY_ROOT) -> list[str]:
    """Fail when the guard cannot see the tree it is meant to protect.

    Every path this module scans is resolved relative to the repository root.
    Run it against a different layout - a documentation-only repository, a
    synced subtree, an unexpected working directory - and each lookup misses,
    the scan finds nothing, and the guard reports success without having
    examined a single file. That silent pass is the failure mode worth
    catching, so treat an empty scope as an error rather than a clean run.
    """
    failures: list[str] = []
    docs_root = root / "docs"
    if not docs_root.is_dir():
        failures.append(
            f"guard scope: no documentation tree at {docs_root} - "
            "the publication guard cannot protect what it cannot see"
        )
    templates_root = root / "website" / "templates"
    if not templates_root.is_dir():
        failures.append(f"guard scope: no website templates at {templates_root}")
    if not public_candidate_files(root):
        failures.append(f"guard scope: no public source files found under {root}")
    return failures


def find_public_reference_leaks(root: Path = REPOSITORY_ROOT) -> list[str]:
    """Find public source pages that reference a private input path or filename."""
    failures: list[str] = []
    for path in public_candidate_files(root):
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
        *find_guard_scope_failures(root),
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
    examined = len(public_candidate_files())
    print(
        f"Public documentation contains no private reference material ({examined} files checked)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
