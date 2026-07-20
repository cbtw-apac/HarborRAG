from __future__ import annotations

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
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{resolved}"
