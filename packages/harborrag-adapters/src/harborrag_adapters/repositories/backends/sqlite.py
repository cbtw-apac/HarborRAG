from __future__ import annotations

import os
from pathlib import Path

SQLITE_MEMORY = ":memory:"


def sqlite_url(database: str) -> str:
    """Build an async SQLAlchemy URL from an explicit SQLite database location."""
    normalized = database.strip()
    if not normalized:
        raise ValueError("SQLite database location cannot be empty")
    if normalized == SQLITE_MEMORY:
        return "sqlite+aiosqlite:///:memory:"
    path = Path(normalized).expanduser()
    resolved = path if path.is_absolute() else Path.cwd() / path
    resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(resolved.parent, 0o700)
    descriptor = os.open(
        resolved,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    os.close(descriptor)
    os.chmod(resolved, 0o600)
    return f"sqlite+aiosqlite:///{resolved}"
