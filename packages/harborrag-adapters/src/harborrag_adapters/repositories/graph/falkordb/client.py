from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from typing import Any

from harborrag_adapters.repositories.lifecycle import AsyncLifecycle

try:
    from falkordb.asyncio import FalkorDB
except ImportError:  # pragma: no cover - optional dependency
    FalkorDB = None


class FalkorDBClient(AsyncLifecycle):
    """Owns the official asynchronous FalkorDB client and selected graph."""

    def __init__(  # noqa: PLR0913 - mirrors provider connection configuration
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
        self._operation_slots = asyncio.BoundedSemaphore(max_connections)
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
        async with self._operation_slots:
            await self.raw.list_graphs()

    async def write(self, statement: str, parameters: Mapping[str, Any]) -> Any:
        """Execute one parameterized FalkorDB write query."""
        async with self._operation_slots:
            return await self.graph.query(statement, params=dict(parameters))

    async def read(self, statement: str, parameters: Mapping[str, Any]) -> Any:
        """Execute one parameterized FalkorDB read-only query."""
        async with self._operation_slots:
            return await self.graph.ro_query(statement, params=dict(parameters))

    async def create_unique_node_constraint(
        self,
        *,
        label: str,
        properties: tuple[str, ...],
    ) -> Any:
        """Create a native FalkorDB unique-node constraint."""

        if not label or not properties:
            raise ValueError("constraint label and properties must be non-empty")
        async with self._operation_slots:
            return await self.raw.execute_command(
                "GRAPH.CONSTRAINT",
                "CREATE",
                self._graph_name,
                "UNIQUE",
                "NODE",
                label,
                "PROPERTIES",
                len(properties),
                *properties,
            )
