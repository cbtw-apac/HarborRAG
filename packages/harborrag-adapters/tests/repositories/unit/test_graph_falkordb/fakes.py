from __future__ import annotations

import asyncio
from typing import Any


def client_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "host": "localhost",
        "port": 6379,
        "username": None,
        "password": None,
        "graph_name": "harborrag",
        "ssl": False,
        "max_connections": 1,
        "connect_timeout_seconds": 1,
        "operation_timeout_seconds": 1,
    }
    base.update(overrides)
    return base


class HeaderItem:
    def __init__(self, name: str | None) -> None:
        self.name = name


class FakeQueryResult:
    def __init__(self, header: list[Any], result_set: list[list[Any]]) -> None:
        self.header = header
        self.result_set = result_set


class FakeGraph:
    def __init__(self) -> None:
        self.query_calls: list[tuple[str, dict[str, Any]]] = []
        self.ro_query_calls: list[tuple[str, dict[str, Any]]] = []
        self.active_calls = 0
        self.maximum_active_calls = 0

    async def query(self, statement: str, params: dict[str, Any]) -> str:
        return await self._record("write", statement, params)

    async def ro_query(self, statement: str, params: dict[str, Any]) -> str:
        return await self._record("read", statement, params)

    async def _record(
        self,
        operation: str,
        statement: str,
        params: dict[str, Any],
    ) -> str:
        calls = self.query_calls if operation == "write" else self.ro_query_calls
        calls.append((statement, params))
        self.active_calls += 1
        self.maximum_active_calls = max(self.maximum_active_calls, self.active_calls)
        try:
            await asyncio.sleep(0)
            return f"{operation}-result"
        finally:
            self.active_calls -= 1


class FalkorDBWithGraph:
    def __init__(self, **options: Any) -> None:
        del options
        self.graph = FakeGraph()

    def select_graph(self, name: str) -> FakeGraph:
        del name
        return self.graph

    async def list_graphs(self) -> list[str]:
        return []


class CountingFalkorDB:
    instances = 0

    def __init__(self, **options: Any) -> None:
        del options
        CountingFalkorDB.instances += 1

    def select_graph(self, name: str) -> object:
        del name
        return object()

    async def list_graphs(self) -> list[str]:
        return []


class FalkorDBPingFails:
    last_instance: FalkorDBPingFails | None = None

    def __init__(self, **options: Any) -> None:
        del options
        self.closed = False
        FalkorDBPingFails.last_instance = self

    def select_graph(self, name: str) -> object:
        del name
        return object()

    async def list_graphs(self) -> list[str]:
        raise RuntimeError("ping failed")

    async def aclose(self) -> None:
        self.closed = True


class FalkorDBDirectClose:
    def __init__(self, **options: Any) -> None:
        del options
        self.closed = False

    def select_graph(self, name: str) -> object:
        del name
        return object()

    async def list_graphs(self) -> list[str]:
        return []

    async def aclose(self) -> None:
        self.closed = True


class SyncCloseConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FalkorDBSyncConnectionCloseOnly:
    def __init__(self, **options: Any) -> None:
        del options
        self.connection = SyncCloseConnection()

    def select_graph(self, name: str) -> object:
        del name
        return object()

    async def list_graphs(self) -> list[str]:
        return []


class FalkorDBWithoutAnyCloseMethod:
    def __init__(self, **options: Any) -> None:
        del options

    def select_graph(self, name: str) -> object:
        del name
        return object()

    async def list_graphs(self) -> list[str]:
        return []


class FakeFalkorDBClient:
    def __init__(self) -> None:
        self.connected = False
        self.write_calls: list[tuple[str, dict[str, Any]]] = []
        self.read_calls: list[tuple[str, dict[str, Any]]] = []
        self.read_results: list[FakeQueryResult] = []
        self.constraint_calls: list[tuple[str, tuple[str, ...]]] = []
        self.ping_error: Exception | None = None

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def ping(self) -> None:
        if self.ping_error is not None:
            raise self.ping_error

    async def write(self, statement: str, parameters: dict[str, Any]) -> None:
        self.write_calls.append((statement, dict(parameters)))

    async def read(self, statement: str, parameters: dict[str, Any]) -> FakeQueryResult:
        self.read_calls.append((statement, dict(parameters)))
        return self.read_results.pop(0)

    async def create_unique_node_constraint(
        self,
        *,
        label: str,
        properties: tuple[str, ...],
    ) -> None:
        self.constraint_calls.append((label, properties))


def raw_node(entity_id: str, tenant_id: str, labels: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "id": entity_id,
        "tenant_id": tenant_id,
        "labels": labels,
        "confidence": None,
        "provenance": {},
        "valid_from": "2024-01-01T00:00:00+00:00",
        "valid_to": None,
        **extra,
    }


def raw_edge(
    edge_id: str,
    tenant_id: str,
    source_id: str,
    target_id: str,
    relationship_type: str = "RELATED_TO",
) -> dict[str, Any]:
    return {
        "id": edge_id,
        "tenant_id": tenant_id,
        "source_id": source_id,
        "target_id": target_id,
        "type": relationship_type,
        "confidence": None,
        "provenance": {},
        "valid_from": "2024-01-01T00:00:00+00:00",
        "valid_to": None,
    }
