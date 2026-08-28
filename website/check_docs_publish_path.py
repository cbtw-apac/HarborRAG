#!/usr/bin/env python3
"""Report which Markdown files reach the published documentation website.

The builder renders a specific, narrow set of sources. Everything else is an
in-tree developer note that no reader of the website will ever see, so editing
one is effort that never reaches an audience. This script names the boundary
so it can be checked rather than remembered.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

#: Root files bridged onto the site by ``builder.site.SiteBuildMixin``.
PUBLISHED_ROOT_FILES = frozenset({"README.md", "CONTRIBUTING.md", "CHANGELOG.md", "SECURITY.md"})

PUBLISHED = "published"
PACKAGE_DETAIL = "package-detail"
INTERNAL = "internal"

CATEGORY_NOTES = {
    PUBLISHED: "rendered on the documentation website",
    PACKAGE_DETAIL: "reserved for detailed package docs; not rendered by the builder yet",
    INTERNAL: "in-tree developer note; never rendered on the website",
}


def _is_package_readme(parts: tuple[str, ...], root: Path) -> bool:
    """Report whether a path is a distribution README the builder renders."""
    if len(parts) != 3 or parts[0] != "packages" or parts[2] != "README.md":
        return False
    return (root / parts[0] / parts[1] / "pyproject.toml").is_file()


def classify(path: str, root: Path = REPOSITORY_ROOT) -> str:
    """Return the publication category for one repository-relative path."""
    parts = tuple(Path(path).as_posix().split("/"))

    if len(parts) == 1 and parts[0] in PUBLISHED_ROOT_FILES:
        return PUBLISHED
    if parts[0] == "docs":
        return PUBLISHED
    if _is_package_readme(parts, root):
        return PUBLISHED
    if len(parts) > 3 and parts[0] == "packages" and parts[2] == "docs":
        return PACKAGE_DETAIL
    return INTERNAL


def tracked_markdown(root: Path = REPOSITORY_ROOT) -> list[str]:
    """Return every tracked Markdown path, repository-relative and sorted."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
            capture_output=True,
            text=True,
            cwd=root,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return sorted(line for line in result.stdout.splitlines() if line.strip())


def group(paths: list[str], root: Path = REPOSITORY_ROOT) -> dict[str, list[str]]:
    """Bucket paths by publication category."""
    grouped: dict[str, list[str]] = {PUBLISHED: [], PACKAGE_DETAIL: [], INTERNAL: []}
    for path in paths:
        grouped[classify(path, root)].append(path)
    return grouped


def report(grouped: dict[str, list[str]], *, list_internal: bool) -> None:
    """Print a human-readable summary of the publication boundary."""
    print("Documentation publication boundary")
    for category in (PUBLISHED, PACKAGE_DETAIL, INTERNAL):
        entries = grouped[category]
        print(f"  {category:<15} {len(entries):>4}  ({CATEGORY_NOTES[category]})")

    if list_internal and grouped[INTERNAL]:
        print("\nMarkdown that never reaches the website:")
        for path in grouped[INTERNAL]:
            print(f"  - {path}")
        print(
            "\nMove reader-facing content into docs/ or packages/<name>/docs/. "
            "Keep only contributor notes here."
        )


def main(argv: list[str] | None = None) -> int:
    """Run the publication-boundary report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths", nargs="*", help="Paths to classify (default: all tracked Markdown)"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when any examined Markdown is an in-tree developer note",
    )
    parser.add_argument(
        "--changed-from",
        metavar="REF",
        help="Classify only Markdown changed since REF (requires full git history)",
    )
    args = parser.parse_args(argv)

    if args.paths:
        paths = sorted(args.paths)
    elif args.changed_from:
        diff = subprocess.run(
            ["git", "diff", "--name-only", f"{args.changed_from}...HEAD", "--", "*.md"],
            capture_output=True,
            text=True,
            cwd=REPOSITORY_ROOT,
            check=False,
        )
        if diff.returncode != 0:
            print(f"Could not diff against {args.changed_from}: {diff.stderr.strip()}")
            return 1
        paths = sorted(line for line in diff.stdout.splitlines() if line.strip())
    else:
        paths = tracked_markdown()

    grouped = group(paths)
    report(grouped, list_internal=bool(args.paths or args.changed_from) or args.strict)

    if args.strict and grouped[INTERNAL]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
