"""An audit resource connection must not provision buckets, graphs or schema."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.topology import composition


@pytest.mark.asyncio
async def test_readonly_runtime_skips_all_provisioning(monkeypatch):
    control = SimpleNamespace(
        connect=AsyncMock(),
        close=AsyncMock(),
        topology=SimpleNamespace(),
        document_versions=SimpleNamespace(),
    )
    store = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(), ensure_buckets=AsyncMock())
    graph = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    migrate = Mock(side_effect=AssertionError("read-only audit attempted schema migration"))
    monkeypatch.setattr(
        "harborrag_adapters.repositories.database.control_plane.migrations.run_migrations", migrate
    )
    monkeypatch.setattr(composition, "build_ingestion_control", lambda settings: control)
    monkeypatch.setattr(composition, "build_object_store", lambda settings: store)
    monkeypatch.setattr(composition, "build_topology_repository", lambda settings: graph)
    async with composition.connect_topology_runtime(
        RuntimeSettings(), provision_graph=False
    ) as runtime:
        assert runtime.control is control
        assert runtime.projection is graph
    migrate.assert_not_called()
    store.ensure_buckets.assert_not_awaited()
    graph.connect.assert_awaited_once_with(provision=False)
    graph.close.assert_awaited_once()
    store.close.assert_awaited_once()
    control.close.assert_awaited_once()
