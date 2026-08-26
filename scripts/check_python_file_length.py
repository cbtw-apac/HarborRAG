"""Fail when a Python file reaches 350 physical lines.

The count includes imports, comments, docstrings, blanks, and every other
physical line. Both tracked and non-ignored untracked files are checked so the
gate is useful before staging changes.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_LINES = 349
# Alembic revisions are generated, append-only, and identified by their revision graph.
# Splitting one would break that graph without making anything easier to read, so the
# gate skips them rather than inviting a per-file suppression comment.
EXEMPT_PARTS = (("alembic", "versions"),)


def is_exempt(path: Path, *, root: Path = REPO_ROOT) -> bool:
    """Report whether a path is excluded from the physical line-length gate."""

    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return any(all(part in parts for part in exempt_parts) for exempt_parts in EXEMPT_PARTS)


def python_files(root: Path = REPO_ROOT) -> tuple[Path, ...]:
    """Return tracked and non-ignored untracked Python files."""

    result = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.py",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return tuple(
        root / relative
        for relative in result.stdout.decode("utf-8").split("\0")
        if relative and (root / relative).is_file()
    )


def physical_line_count(path: Path) -> int:
    """Count physical lines without normalizing newline style."""

    content = path.read_bytes()
    return content.count(b"\n") + int(bool(content) and not content.endswith(b"\n"))


def oversized_files(
    paths: Iterable[Path],
    *,
    maximum: int = MAX_LINES,
    root: Path = REPO_ROOT,
) -> tuple[str, ...]:
    """Describe files whose physical line count exceeds the maximum."""

    failures: list[str] = []
    for path in sorted(paths):
        if is_exempt(path, root=root):
            continue
        count = physical_line_count(path)
        if count > maximum:
            try:
                display = path.relative_to(root)
            except ValueError:
                display = path
            failures.append(f"{display.as_posix()}: {count} lines (maximum {maximum})")
    return tuple(failures)


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    failures = oversized_files(python_files())
    if failures:
        print("Python file-length gate failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"All Python files are at most {MAX_LINES} physical lines.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
