from __future__ import annotations

import os
from pathlib import Path

SQLITE_MEMORY = ":memory:"


def _resolved(database: str) -> Path | None:
    normalized = database.strip()
    if not normalized:
        raise ValueError("SQLite database location cannot be empty")
    if normalized == SQLITE_MEMORY:
        return None
    path = Path(normalized).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def sqlite_url(database: str) -> str:
    """Build an async SQLAlchemy URL from an explicit SQLite database location.

    Pure: this creates nothing and changes no permissions. Call
    ``prepare_sqlite_database`` where the file itself has to exist.
    """

    resolved = _resolved(database)
    if resolved is None:
        return "sqlite+aiosqlite:///:memory:"
    return f"sqlite+aiosqlite:///{resolved}"


def prepare_sqlite_database(database: str) -> str:
    """Create the database file owner-only, and return its URL.

    Kept apart from ``sqlite_url`` because resolving a configured location
    should not touch the filesystem. While the two were one function, merely
    building a URL created directories and reset the mode of whatever parent the
    operator had chosen -- stripping group and other access from a shared
    directory, or raising ``PermissionError`` on one the process does not own.

    The parent's mode is now set only when this creates the directory. An
    existing directory belongs to the operator; the database file is ours, so it
    is always narrowed to 0600.
    """

    resolved = _resolved(database)
    if resolved is None:
        return "sqlite+aiosqlite:///:memory:"
    parent = resolved.parent
    created = not parent.exists()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if created:
        os.chmod(parent, 0o700)
    descriptor = os.open(
        resolved,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    os.close(descriptor)
    os.chmod(resolved, 0o600)
    return f"sqlite+aiosqlite:///{resolved}"
