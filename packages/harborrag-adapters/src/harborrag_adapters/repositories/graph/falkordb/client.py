"""FalkorDB client that owns the connection and translates redis outages.

Only redis connection and timeout errors are translated. Redis ``ResponseError`` passes
through, because provisioning tells "already exists" apart by its message.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from harborrag_adapters.repositories.errors import (
    MissingOptionalDependencyError,
    StorageErrorContext,
    StorageFailure,
    storage_failure_error,
)
from harborrag_adapters.repositories.lifecycle import AsyncLifecycle
from harborrag_core.storage import StorageFamily

try:
    from falkordb.asyncio import FalkorDB
except ImportError:  # pragma: no cover - optional dependency
    FalkorDB = None

_RedisConnectionError: Any
_RedisTimeoutError: Any
try:
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError
except ImportError:  # pragma: no cover - installed with falkordb
    _RedisConnectionError = _RedisTimeoutError = ()
else:
    _RedisConnectionError = RedisConnectionError
    _RedisTimeoutError = RedisTimeoutError


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
            raise MissingOptionalDependencyError("FalkorDB")
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
            execute = getattr(self.raw, "execute_command", None)
            with self._translated("ping"):
                if execute is not None:
                    # Reader credentials need PING, not tenant-wide GRAPH.LIST access.
                    await execute("PING")
                else:
                    await self.raw.list_graphs()

    async def write(self, statement: str, parameters: Mapping[str, Any]) -> Any:
        """Execute one parameterized FalkorDB write query."""
        async with self._operation_slots:
            with self._translated("write"):
                return await self.graph.query(statement, params=dict(parameters))

    async def read(self, statement: str, parameters: Mapping[str, Any]) -> Any:
        """Execute one parameterized FalkorDB read-only query."""
        async with self._operation_slots:
            with self._translated("read"):
                return await self.graph.ro_query(statement, params=dict(parameters))

    @contextmanager
    def _translated(self, operation: str) -> Iterator[None]:
        """Raise a redis connection or timeout error as a transient storage error."""
        try:
            yield
        except _RedisTimeoutError as error:
            raise storage_failure_error(StorageFailure.TIMEOUT, self._context(operation)) from error
        except _RedisConnectionError as error:
            raise storage_failure_error(
                StorageFailure.UNAVAILABLE, self._context(operation)
            ) from error

    def _context(self, operation: str) -> StorageErrorContext:
        return StorageErrorContext(
            family=StorageFamily.GRAPH,
            backend=self.backend,
            instance_name=self._graph_name,
            operation=operation,
        )

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
            with self._translated("create_unique_node_constraint"):
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

    async def drop_unique_node_constraint(
        self,
        *,
        label: str,
        properties: tuple[str, ...],
    ) -> Any:
        """Drop a native FalkorDB unique-node constraint."""

        if not label or not properties:
            raise ValueError("constraint label and properties must be non-empty")
        async with self._operation_slots:
            with self._translated("drop_unique_node_constraint"):
                return await self.raw.execute_command(
                    "GRAPH.CONSTRAINT",
                    "DROP",
                    self._graph_name,
                    "UNIQUE",
                    "NODE",
                    label,
                    "PROPERTIES",
                    len(properties),
                    *properties,
                )
