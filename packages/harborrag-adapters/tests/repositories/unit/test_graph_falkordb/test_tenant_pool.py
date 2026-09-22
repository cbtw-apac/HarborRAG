"""Trusted tenant graph selection, least-privilege reads and bounded lifetimes."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr, ValidationError

from harborrag_adapters.repositories.graph.falkordb import (
    FalkorDBGraphConfig,
    FalkorKnowledgeGraphRepository,
)
from harborrag_adapters.repositories.graph.falkordb.tenant_pool import (
    ConfiguredGraphClientFactory,
    TenantGraphClientPool,
    TenantGraphRegistry,
)
from harborrag_adapters.repositories.graph.falkordb.topology import FalkorTopologyRepository
from harborrag_core.storage import StorageOperationContext

from .fakes import FakeFalkorDBClient, FakeQueryResult, HeaderItem


class FakeFactory:
    def __init__(self):
        self.created = []

    def create(self, graph_name, *, readonly):
        client = FakeFalkorDBClient()
        self.created.append((graph_name, readonly, client))
        return client


def configuration(**kwargs):
    return FalkorDBGraphConfig(tenant_isolation=True, **kwargs)


def context(tenant="tenant-a"):
    return StorageOperationContext.system(tenant)


def test_registry_is_stable_opaque_and_uses_only_trusted_tenant_context():
    registry = TenantGraphRegistry("harbor")
    assert registry.graph_for(context()) == registry.graph_for(context())
    assert registry.graph_for(context()) != registry.graph_for(context("tenant-b"))
    assert "tenant-a" not in registry.graph_for(context())
    assert len(registry.graph_for(context())) == len("harbor_") + 64
    with pytest.raises(ValidationError):
        configuration(tenant_graph_prefix="graph) DETACH DELETE n")


@pytest.mark.asyncio
async def test_read_connection_never_provisions_and_writer_is_separate():
    factory = FakeFactory()
    provisioner = AsyncMock()
    pool = TenantGraphClientPool(configuration(), provisioner=provisioner, factory=factory)
    await pool.connect()
    assert not factory.created
    reader = await pool.database_for(context())
    assert factory.created[0][1] is True
    provisioner.assert_not_called()
    writer = await pool.database_for(context(), write=True)
    assert writer is not reader and factory.created[1][1] is False
    provisioner.assert_awaited_once_with(writer)
    assert factory.created[0][0] == factory.created[1][0]
    assert await pool.database_for(context()) is reader
    assert await pool.database_for(context(), write=True) is writer
    await pool.close()
    assert all(not client.connected for _, _, client in factory.created)


@pytest.mark.asyncio
async def test_pool_capacity_does_not_evict_active_connections():
    factory = FakeFactory()
    pool = TenantGraphClientPool(
        configuration(max_cached_tenants=1), provisioner=AsyncMock(), factory=factory
    )
    first = await pool.database_for(context())
    with pytest.raises(RuntimeError, match="capacity"):
        await pool.database_for(context("tenant-b"))
    assert first.connected
    await pool.database_for(context(), write=True)
    assert len(factory.created) == 2
    await pool.close()
    await pool.close()
    with pytest.raises(RuntimeError, match="closed"):
        await pool.database_for(context())


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_isolation", [False, True])
async def test_pool_retries_failed_close_without_reopening(tenant_isolation):
    factory = FakeFactory()
    pool = TenantGraphClientPool(
        FalkorDBGraphConfig(tenant_isolation=tenant_isolation),
        provisioner=AsyncMock(),
        factory=factory,
    )
    await pool.connect()
    first = await pool.database_for(context())
    first.close = AsyncMock(side_effect=[OSError("close failed"), None])
    if tenant_isolation:
        second = await pool.database_for(context("tenant-b"))
        second.close = AsyncMock()

    with pytest.raises(OSError, match="close failed"):
        await pool.close()
    with pytest.raises(RuntimeError, match="closed"):
        await pool.database_for(context())
    with pytest.raises(RuntimeError, match="closed"):
        await pool.connect()

    await pool.close()
    await pool.close()

    assert first.close.await_count == 2
    if tenant_isolation:
        second.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_concurrent_resolution_constructs_and_provisions_only_one_writer():
    factory = FakeFactory()
    provisioner = AsyncMock()
    pool = TenantGraphClientPool(configuration(), provisioner=provisioner, factory=factory)
    clients = await asyncio.gather(*(pool.database_for(context(), write=True) for _ in range(20)))
    assert len({id(client) for client in clients}) == 1
    provisioner.assert_awaited_once()
    await pool.close()


@pytest.mark.asyncio
async def test_failed_provision_is_closed_and_not_cached():
    factory = FakeFactory()
    pool = TenantGraphClientPool(
        configuration(),
        provisioner=AsyncMock(side_effect=RuntimeError("DDL failed")),
        factory=factory,
    )
    with pytest.raises(RuntimeError, match="DDL failed"):
        await pool.database_for(context(), write=True)
    assert not factory.created[0][2].connected
    reader = await pool.database_for(context())
    assert reader.connected and len(factory.created) == 2
    await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository_type", [FalkorKnowledgeGraphRepository, FalkorTopologyRepository]
)
async def test_both_repositories_use_distinct_physical_graphs_for_tenants(repository_type):
    factory = FakeFactory()
    graph = repository_type(configuration(), client_factory=factory)
    await graph.connect(provision=False)
    first = await graph.database_for(context())
    second = await graph.database_for(context("tenant-b"))
    assert first is not second
    assert factory.created[0][0] != factory.created[1][0]
    assert not first.write_calls and not second.write_calls
    assert not first.constraint_calls and not second.constraint_calls
    await graph.close()


@pytest.mark.asyncio
async def test_knowledge_counts_use_reader_and_tenant_parameters_only():
    factory = FakeFactory()
    graph = FalkorKnowledgeGraphRepository(configuration(), client_factory=factory)
    reader = await graph.database_for(context())
    reader.read_results = [FakeQueryResult([HeaderItem("item_count")], [[1]]) for _ in range(2)]
    assert await graph.tenant_projection_counts(context=context()) == (1, 1)
    assert len(reader.read_calls) == 2 and not reader.write_calls
    assert all(parameters["tenant_id"] == "tenant-a" for _, parameters in reader.read_calls)


def test_unscoped_injected_client_cannot_bypass_isolated_routing():
    with pytest.raises(ValueError, match="tenant-aware"):
        FalkorKnowledgeGraphRepository(configuration(), client=FakeFalkorDBClient())


def test_factory_uses_separate_reader_credentials_without_writer_password_fallback():
    factory = ConfiguredGraphClientFactory(
        configuration(
            username="indexer",
            password=SecretStr("index-password"),
            read_username="reader",
            read_password=SecretStr("read-password"),
        )
    )
    reader = factory.create("trusted_graph", readonly=True)
    writer = factory.create("trusted_graph", readonly=False)
    assert reader._connection["username"] == "reader"
    assert reader._connection["password"] == "read-password"
    assert writer._connection["username"] == "indexer"
    assert writer._connection["password"] == "index-password"
