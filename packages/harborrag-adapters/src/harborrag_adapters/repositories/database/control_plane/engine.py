"""Async engine + session factory for the control-plane DB (ST5).

DSN comes from HARBORRAG_CONTROL_DB_URL (composition layer); SQLite via
aiosqlite for dev, Postgres via asyncpg in production.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DEFAULT_DSN = "sqlite+aiosqlite:///./harborrag_control.db"


def create_control_plane_engine(dsn: str = DEFAULT_DSN) -> AsyncEngine:
    """Async engine for the control-plane database."""
    return create_async_engine(dsn)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory the repositories share; commits are per-repo-call."""
    return async_sessionmaker(engine, expire_on_commit=False)
