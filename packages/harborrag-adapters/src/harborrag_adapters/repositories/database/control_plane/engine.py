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
from sqlalchemy.pool import NullPool

DEFAULT_DSN = "sqlite+aiosqlite:///./harborrag_control.db"


def create_control_plane_engine(dsn: str = DEFAULT_DSN) -> AsyncEngine:
    """Async engine for the control-plane database.

    SQLite goes through NullPool: aiosqlite backs each connection with its own
    worker thread, and an idle *pooled* connection that outlives the event
    loop it was checked out on is a documented source of "Event loop is
    closed" errors from that thread. NullPool closes the connection as soon
    as it's released instead of holding it idle, and SQLite (single-writer,
    file-local) gets no real benefit from pooling anyway. Postgres (asyncpg)
    keeps SQLAlchemy's default pool, where connection reuse pays off.
    """
    if dsn.startswith("sqlite"):
        return create_async_engine(dsn, poolclass=NullPool)
    return create_async_engine(dsn)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory the repositories share; commits are per-repo-call."""
    return async_sessionmaker(engine, expire_on_commit=False)
