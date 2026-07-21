from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

from harborrag_adapters.repositories.lifecycle import AsyncLifecycle

try:
    from falkordb.asyncio import FalkorDB  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - optional dependency
    FalkorDB = None


class FalkorDBClient(AsyncLifecycle):
    """Owns the official asynchronous FalkorDB client and selected graph."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        graph_name: str,
        ssl: bool,
        max_connections: int,
        connect_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> None:
        if FalkorDB is None:
            raise ImportError("FalkorDB is not installed")
        self._connection = {
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "ssl": ssl,
            "max_connections": max_connections,
            "socket_connect_timeout": connect_timeout_seconds,
            "socket_timeout": operation_timeout_seconds,
        }
        self._graph_name = graph_name
        self._database: Any = None
        self._graph: Any = None

    @property
    def backend(self) -> str:
        return "falkordb"

    @property
    def raw(self) -> Any:
        if self._database is None:
            raise RuntimeError("FalkorDB client is not connected")
        return self._database

    @property
    def graph(self) -> Any:
        if self._graph is None:
            raise RuntimeError("FalkorDB client is not connected")
        return self._graph

    async def connect(self) -> None:
        if self._database is None:
            kwargs = {key: value for key, value in self._connection.items() if value is not None}
            self._database = FalkorDB(**kwargs)
            self._graph = self._database.select_graph(self._graph_name)
            try:
                await self.ping()
            except BaseException:
                await self.close()
                raise

    async def close(self) -> None:
        if self._database is not None:
            close = getattr(self._database, "aclose", None)
            if close is None:
                connection = getattr(self._database, "connection", None)
                close = getattr(connection, "aclose", None) or getattr(connection, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
            self._database = None
            self._graph = None

    async def ping(self) -> None:
        await self.raw.list_graphs()

    async def write(self, statement: str, parameters: Mapping[str, Any]) -> Any:
        """Execute one parameterized FalkorDB write query."""
        return await self.graph.query(statement, params=dict(parameters))

    async def read(self, statement: str, parameters: Mapping[str, Any]) -> Any:
        """Execute one parameterized FalkorDB read-only query."""
        return await self.graph.ro_query(statement, params=dict(parameters))
