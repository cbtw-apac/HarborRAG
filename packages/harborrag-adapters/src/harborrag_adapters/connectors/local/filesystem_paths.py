"""Path, checksum, MIME and glob helpers for local files.

Handles:
- Path resolution and root-relative paths
- Hidden-path, pattern, glob and subtree matching
- Stat-based signatures and timestamps
"""

from __future__ import annotations

import mimetypes
import os
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path

from harborrag_adapters.connectors.file_filters import normalize_extension as normalize_extension

# Windows has no openat()/O_NOFOLLOW-per-component equivalent
SUPPORTS_DIR_FD = os.open in os.supports_dir_fd

DEFAULT_EXCLUDED_DIR_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}


def resolve_path(value: str | Path) -> Path:
    """Return the path with ``~`` expanded and symlinks resolved; it need not exist."""
    return Path(value).expanduser().resolve(strict=False)


def file_extension(path: Path) -> str:
    """Return the path's lowercased suffix."""
    return path.suffix.lower()


def guess_mime_type(path: Path) -> str:
    """Return the guessed MIME type of a path, defaulting to ``application/octet-stream``."""
    mime_type, _ = mimetypes.guess_type(str(path))
    return mime_type or "application/octet-stream"


def relative_path(path: Path, root_path: Path) -> str:
    """Return the POSIX path relative to ``root_path``, or the absolute POSIX path if outside."""
    try:
        return path.relative_to(root_path).as_posix()
    except ValueError:
        return path.as_posix()


def is_hidden_path(path: Path, root_path: Path) -> bool:
    """Return whether any component of the root-relative path starts with a dot."""
    relative = relative_path(path, root_path)
    return any(part.startswith(".") for part in Path(relative).parts)


def stat_datetime(timestamp: float) -> datetime:
    """Return a filesystem timestamp as a UTC datetime."""
    return datetime.fromtimestamp(timestamp, tz=UTC)


def stat_signature(path: Path) -> str:
    """Return ``stat:<size>:<mtime ns>`` as a cheap change signature."""
    stat = path.stat()
    mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
    return f"stat:{stat.st_size}:{mtime_ns}"


def matches_pattern(path: Path, root_path: Path, pattern: str | None) -> bool:
    """Return whether a path matches a glob or contains the pattern, case-insensitively."""
    if not pattern:
        return True
    relative = relative_path(path, root_path).lower()
    name = path.name.lower()
    normalized = pattern.replace("\\", "/").lower()
    if any(char in normalized for char in "*?[]"):
        return fnmatch(relative, normalized) or fnmatch(name, normalized)
    return normalized in relative or normalized in name


def matches_globs(path: Path, root_path: Path, patterns: list[str]) -> bool:
    """Return whether the root-relative path matches any glob, case-insensitively."""
    # TODO: match ``**/`` globs against root-level files and support ``**`` fully.
    relative = relative_path(path, root_path).lower()
    return any(fnmatch(relative, pattern.replace("\\", "/").lower()) for pattern in patterns)


def path_in_scope(path: Path, root_path: Path, candidate: str) -> bool:
    """Return whether a path is within the ``candidate`` subtree, case-insensitively."""
    normalized = candidate.replace("\\", "/").strip("/").lower()
    if not normalized:
        return True
    relative = relative_path(path, root_path).lower()
    return relative == normalized or relative.startswith(f"{normalized}/")
