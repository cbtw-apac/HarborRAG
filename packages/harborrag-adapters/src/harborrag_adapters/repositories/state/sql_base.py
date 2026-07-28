from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .sql_backend import SQLStateBackend


class SQLStoreBase:
    """Holds the shared SQL backend used by cohesive state products."""

    def __init__(self, backend: SQLStateBackend) -> None:
        self._backend = backend
        self._telemetry = backend._telemetry

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        async with self._backend.client.sessions() as session, session.begin():
            yield session
