"""Pure path, checksum, MIME, and filtering helpers for local files."""

from __future__ import annotations

import mimetypes
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path

from harborrag_adapters.connectors.file_filters import normalize_extension as normalize_extension

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
    """Expand and resolve a local path without requiring it to exist."""
    return Path(value).expanduser().resolve(strict=False)


def file_extension(path: Path) -> str:
    """Return a lowercased suffix for a local path."""
    return path.suffix.lower()


def guess_mime_type(path: Path) -> str:
    """Infer a MIME type from a local path."""
    mime_type, _ = mimetypes.guess_type(str(path))
    return mime_type or "application/octet-stream"


def relative_path(path: Path, root_path: Path) -> str:
    """Return a POSIX-style path relative to root when possible."""
    try:
        return path.relative_to(root_path).as_posix()
    except ValueError:
        return path.as_posix()


def is_hidden_path(path: Path, root_path: Path) -> bool:
    """Return whether any relative path component is hidden."""
    relative = relative_path(path, root_path)
    return any(part.startswith(".") for part in Path(relative).parts)


def stat_datetime(timestamp: float) -> datetime:
    """Convert a filesystem timestamp to a timezone-aware datetime."""
    return datetime.fromtimestamp(timestamp, tz=UTC)


def stat_signature(path: Path) -> str:
    """Build a cheap change signature from file size and mtime."""
    stat = path.stat()
    mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
    return f"stat:{stat.st_size}:{mtime_ns}"


def matches_pattern(path: Path, root_path: Path, pattern: str | None) -> bool:
    """Return whether a path matches a glob-like pattern or substring."""
    if not pattern:
        return True
    relative = relative_path(path, root_path).lower()
    name = path.name.lower()
    normalized = pattern.replace("\\", "/").lower()
    if any(char in normalized for char in "*?[]"):
        return fnmatch(relative, normalized) or fnmatch(name, normalized)
    return normalized in relative or normalized in name


def matches_globs(path: Path, root_path: Path, patterns: list[str]) -> bool:
    """Return whether a relative path matches any glob pattern (case-insensitive)."""
    relative = relative_path(path, root_path).lower()
    return any(fnmatch(relative, pattern.replace("\\", "/").lower()) for pattern in patterns)


def path_in_scope(path: Path, root_path: Path, candidate: str) -> bool:
    """Return whether a path is within a configured include/exclude subtree (case-insensitive)."""
    normalized = candidate.replace("\\", "/").strip("/").lower()
    if not normalized:
        return True
    relative = relative_path(path, root_path).lower()
    return relative == normalized or relative.startswith(f"{normalized}/")
