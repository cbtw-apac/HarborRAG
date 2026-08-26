from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, text
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.types import TypeDecorator

from harborrag_adapters.repositories.lifecycle import AsyncLifecycle


class UTCDateTime(TypeDecorator[datetime]):
    """Guarantees UTC-aware datetimes survive a round-trip through any SQL dialect.

    SQLite silently drops tzinfo on read even for DateTime(timezone=True) columns,
    which breaks aware/naive comparisons against datetime.now(UTC); this normalizes
    read values back to UTC so behavior is consistent across dialects.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Normalize aware values to UTC and reject ambiguous naive datetimes."""
        del dialect
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("repository datetimes must be timezone-aware")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Re-attach UTC when a database driver returns a naive datetime."""
        del dialect
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class SQLAlchemyDBClient(AsyncLifecycle):
    """Owns an async SQLAlchemy engine without exposing it across HarborRAG layers."""

    def __init__(
        self,
        *,
        backend: str,
        url: str,
        pool_size: int | None,
        max_overflow: int | None,
        pool_recycle_seconds: int,
        echo: bool,
    ) -> None:
        self._backend = backend
        self._url = url
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._pool_recycle_seconds = pool_recycle_seconds
        self._echo = echo
        self._engine: AsyncEngine | None = None
        self._sessions: async_sessionmaker[AsyncSession] | None = None

    @property
    def backend(self) -> str:
        return self._backend

    @staticmethod
    def require_url_prefix(url: str, prefix: str, backend: str) -> None:
        """Reject URLs that cannot use the provider's asynchronous dialect."""
        if not url.startswith(prefix):
            raise ValueError(f"{backend} URL must use {prefix.removesuffix('://')}")

    @property
    def raw(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError(f"{self._backend} database client is not connected")
        return self._engine

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        if self._sessions is None:
            raise RuntimeError(f"{self._backend} database client is not connected")
        return self._sessions

    async def connect(self) -> None:
        if self._engine is not None:
            return
        kwargs: dict[str, Any] = {
            "echo": self._echo,
            "pool_pre_ping": True,
            "pool_recycle": self._pool_recycle_seconds,
        }
        if self._pool_size is not None:
            kwargs["pool_size"] = self._pool_size
        if self._max_overflow is not None:
            kwargs["max_overflow"] = self._max_overflow
        self._engine = create_async_engine(self._url, **kwargs)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        engine = self._engine
        self._engine = None
        self._sessions = None
        if engine is not None:
            await engine.dispose(close=True)
            await asyncio.sleep(0)

    async def ping(self) -> None:
        async with self.raw.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def create_schema(self, metadata: Any) -> None:
        """Create a supplied SQLAlchemy metadata model for local development only."""
        async with self.raw.begin() as connection:
            await connection.run_sync(metadata.create_all)
