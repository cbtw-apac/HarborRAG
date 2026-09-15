"""Operators can find drift and remove only rebuildable, retired projections."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from topology_service_support import Harness

from harborrag_runtime.topology import maintenance


def connect(harness, monkeypatch):
    @asynccontextmanager
    async def runtime(_settings, *, provision_graph):
        assert not provision_graph
        yield SimpleNamespace(
            control=harness.control,
            projection=harness.graph,
            cleanup_derived=None,
        )

    monkeypatch.setattr(maintenance, "connect_topology_runtime", runtime)


@pytest.mark.asyncio
async def test_audit_detects_missing_projection_without_reextracting(tmp_path, monkeypatch):
    async with Harness(tmp_path) as h:
        await h.publish()
        result = await h.service.run_once("DEFAULT")
        connect(h, monkeypatch)
        report = await maintenance.audit(None, "DEFAULT")
        assert report["builds"] == [
            {"build_id": result.build_id, "verified": True, "eligible": True}
        ]
        h.graph.builds.clear()
        report = await maintenance.audit(None, "DEFAULT")
        assert report["builds"][0]["verified"] is False
        assert h.model.calls == 1


@pytest.mark.asyncio
async def test_cleanup_is_dry_run_by_default_and_retains_canonical_artifacts(tmp_path, monkeypatch):
    async with Harness(tmp_path) as h:
        await h.publish()
        result = await h.service.run_once("DEFAULT")
        connect(h, monkeypatch)
        assert not (await maintenance.cleanup(None, "DEFAULT", apply=True))["candidates"]
        await h.control.topology.configure_policy(h.policy.model_copy(update={"enabled": False}))
        assert not (await maintenance.cleanup(None, "DEFAULT"))["removed_projections"]
        assert result.build_id in h.graph.builds
        applied = await maintenance.cleanup(None, "DEFAULT", apply=True)
        assert applied["removed_projections"] == [result.build_id]
        assert result.build_id not in h.graph.builds
        assert await h.control.topology.get_build("DEFAULT", result.build_id) is not None
        assert h.store._objects
        assert h.model.calls == 1


@pytest.mark.asyncio
async def test_maintenance_cursor_can_advance_over_retained_canonical_rows(tmp_path, monkeypatch):
    async with Harness(tmp_path) as h:
        await h.publish()
        result = await h.service.run_once("DEFAULT")
        connect(h, monkeypatch)
        assert not (await maintenance.audit(None, "DEFAULT", after_build_id=result.build_id))[
            "builds"
        ]
        await h.control.topology.configure_policy(h.policy.model_copy(update={"enabled": False}))
        report = await maintenance.cleanup(None, "DEFAULT", after_build_id=result.build_id)
        assert not report["candidates"]
