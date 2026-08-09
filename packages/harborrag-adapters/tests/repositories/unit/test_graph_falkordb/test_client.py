from __future__ import annotations

import asyncio

import pytest

from harborrag_adapters.repositories.graph.falkordb import (
    client as falkordb_client_module,
)
from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient

from .fakes import (
    CountingFalkorDB,
    FalkorDBDirectClose,
    FalkorDBPingFails,
    FalkorDBSyncConnectionCloseOnly,
    FalkorDBWithGraph,
    FalkorDBWithoutAnyCloseMethod,
    client_kwargs,
)


@pytest.mark.asyncio
async def test_write_and_read_delegate_to_the_selected_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", FalkorDBWithGraph)
    client = FalkorDBClient(**client_kwargs())

    await client.connect()
    write_result = await client.write("CREATE (n)", {"a": 1})
    read_result = await client.read("MATCH (n) RETURN n", {"b": 2})

    assert write_result == "write-result"
    assert read_result == "read-result"
    assert client.graph.query_calls == [("CREATE (n)", {"a": 1})]
    assert client.graph.ro_query_calls == [("MATCH (n) RETURN n", {"b": 2})]


@pytest.mark.asyncio
async def test_operations_wait_for_an_available_connection_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", FalkorDBWithGraph)
    client = FalkorDBClient(**client_kwargs(max_connections=1))
    await client.connect()

    await asyncio.gather(
        client.write("CREATE (n)", {}),
        client.read("MATCH (n) RETURN n", {}),
        client.write("CREATE (m)", {}),
    )

    assert client.graph.maximum_active_calls == 1


def test_backend_property_reports_falkordb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", object)
    client = FalkorDBClient(**client_kwargs())
    assert client.backend == "falkordb"


def test_raw_and_graph_properties_raise_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", object)
    client = FalkorDBClient(**client_kwargs())
    with pytest.raises(RuntimeError):
        _ = client.raw
    with pytest.raises(RuntimeError):
        _ = client.graph


@pytest.mark.asyncio
async def test_close_without_connect_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", object)
    client = FalkorDBClient(**client_kwargs())
    await client.close()


@pytest.mark.asyncio
async def test_connect_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    CountingFalkorDB.instances = 0
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", CountingFalkorDB)
    client = FalkorDBClient(**client_kwargs())

    await client.connect()
    await client.connect()

    assert CountingFalkorDB.instances == 1


@pytest.mark.asyncio
async def test_connect_failure_closes_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", FalkorDBPingFails)
    client = FalkorDBClient(**client_kwargs())

    with pytest.raises(RuntimeError, match="ping failed"):
        await client.connect()

    assert FalkorDBPingFails.last_instance is not None
    assert FalkorDBPingFails.last_instance.closed is True
    with pytest.raises(RuntimeError):
        _ = client.raw


@pytest.mark.asyncio
async def test_close_prefers_database_level_aclose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", FalkorDBDirectClose)
    client = FalkorDBClient(**client_kwargs())
    await client.connect()
    database = client.raw

    await client.close()

    assert database.closed is True
    with pytest.raises(RuntimeError):
        _ = client.raw


@pytest.mark.asyncio
async def test_close_falls_back_to_synchronous_connection_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", FalkorDBSyncConnectionCloseOnly)
    client = FalkorDBClient(**client_kwargs())
    await client.connect()
    connection = client.raw.connection

    await client.close()

    assert connection.closed is True


def test_falkordb_client_requires_sdk_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", None)

    with pytest.raises(ImportError, match="FalkorDB is not installed"):
        FalkorDBClient(**client_kwargs())


@pytest.mark.asyncio
async def test_close_is_a_noop_when_no_close_method_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(falkordb_client_module, "FalkorDB", FalkorDBWithoutAnyCloseMethod)
    client = FalkorDBClient(**client_kwargs())
    await client.connect()

    await client.close()

    with pytest.raises(RuntimeError):
        _ = client.raw
