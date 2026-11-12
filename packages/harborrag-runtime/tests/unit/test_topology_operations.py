"""Thin operator entry points preserve the topology authority boundaries."""

from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from harborrag_core.base import utc_now
from harborrag_core.topology import ExtractionProfile, TopologyPolicy
from harborrag_core.topology.config import TenantIndexingConfig, TenantIndexingState
from harborrag_core.topology.permissions import (
    PermissionCoverageCounts,
    PermissionCoverageReport,
    ResolvedPermissionSnapshot,
)
from harborrag_core.topology.resolution import ResolutionRequest
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.topology import operations, security_operations


def _connect(value):
    @asynccontextmanager
    async def connect(*_args, **_kwargs):
        yield value

    return connect


def _profile():
    return ExtractionProfile(
        model="model", deployment_revision="deployment", prompt_digest="prompt"
    )


@pytest.mark.asyncio
async def test_configure_disable_status_and_profile_delegate_to_authority(monkeypatch):
    policy = TopologyPolicy(
        tenant_id="tenant",
        source_scope_id="scope",
        enabled=True,
        profile=_profile(),
    )
    topology = SimpleNamespace(
        configure_policy=AsyncMock(return_value=7),
        reconcile=AsyncMock(return_value=2),
        get_policy=AsyncMock(return_value=policy),
        list_jobs=AsyncMock(
            return_value=(
                SimpleNamespace(
                    job_id="job",
                    document_id="document",
                    document_version_id="version",
                    state="pending",
                    attempts=1,
                    error_code=None,
                    available_at=utc_now(),
                    policy_revision=7,
                ),
            )
        ),
    )
    control = SimpleNamespace(topology=topology)
    monkeypatch.setattr(operations, "connect_topology_authority", _connect(control))
    catalog = object()
    monkeypatch.setattr(operations.HarborChatClientConfig, "from_file", lambda _path: catalog)
    pin = Mock()
    monkeypatch.setattr(operations, "pinned_configuration", pin)
    monkeypatch.setattr(operations, "default_extraction_profile", lambda *_args: _profile())
    settings = RuntimeSettings()

    assert await operations.configure(settings, policy) == {
        "policy_revision": 7,
        "enqueued": 2,
        "enabled": True,
        "fingerprint": policy.fingerprint,
    }
    pin.assert_called_once_with(catalog, policy.profile)
    assert operations.make_profile(settings, "model") == _profile()
    assert (await operations.status(settings, "tenant"))[0]["build_id"] is None
    assert await operations.disable(settings, "tenant", "scope") == {
        "enabled": False,
        "policy_revision": 7,
    }

    topology.get_policy.return_value = None
    with pytest.raises(ValueError, match="no topology policy"):
        await operations.disable(settings, "tenant", "missing")


@pytest.mark.asyncio
async def test_apply_entities_derive_and_resolutions_delegate(monkeypatch, tmp_path):
    settings = RuntimeSettings(graph_build_config_path=str(tmp_path / "graph.yaml"))
    effective = settings.model_copy(update={"topology_poll_seconds": 9})
    config = SimpleNamespace(
        effective_settings=Mock(return_value=effective),
        resolved_ontologies={},
    )
    monkeypatch.setattr(operations.GraphBuildConfig, "from_settings", lambda _settings: config)
    report = SimpleNamespace(as_dict=Mock(return_value={"changed": 1}))
    synchronizer = SimpleNamespace(apply=AsyncMock(return_value=report))
    monkeypatch.setattr(operations, "GraphBuildConfigSynchronizer", Mock(return_value=synchronizer))
    monkeypatch.setattr(operations, "GraphBuildProfileFactory", Mock())

    mention = SimpleNamespace(
        entity_id="entity",
        observation=SimpleNamespace(name="Harbor", entity_type="Product"),
        document_id="document",
        chunk_id="chunk",
        build_id="build",
    )
    decision = SimpleNamespace(model_dump=Mock(return_value={"revision": 1}))
    resolution = SimpleNamespace(model_dump=Mock(return_value={"decision_id": "decision"}))
    topology = SimpleNamespace(
        active_mentions=AsyncMock(return_value=(mention,)),
        record_resolution=AsyncMock(return_value=decision),
        reconcile=AsyncMock(return_value=4),
        list_resolutions=AsyncMock(return_value=(resolution,)),
    )
    control = SimpleNamespace(topology=topology)
    monkeypatch.setattr(operations, "connect_topology_authority", _connect(control))

    applied = await operations.apply_graph_build_config(settings)
    assert applied["synchronization"] == {"changed": 1}
    assert applied["runtime"]["poll_seconds"] == 9
    entities = await operations.entities(settings, "tenant", "Product", principal_id="principal")
    assert entities == [
        {
            "entity_id": "entity",
            "label": "Harbor",
            "type": "Product",
            "document_id": "document",
            "chunk_id": "chunk",
            "build_id": "build",
        }
    ]
    assert topology.active_mentions.call_args.kwargs["labels"] == ("Product",)
    await operations.entities(settings, "tenant")
    assert topology.active_mentions.call_args.kwargs["access"] is None

    runtime = SimpleNamespace(derive=AsyncMock(return_value="ready"))
    monkeypatch.setattr(operations, "connect_topology_runtime", _connect(runtime))
    assert await operations.derive(settings, "tenant", "build") == "ready"

    request = ResolutionRequest(
        tenant_id="tenant",
        decision_id="decision",
        action="merge",
        entity_ids=("one", "two"),
        reason="same entity",
        actor="operator",
    )
    assert await operations.resolve(settings, request) == {
        "decision": {"revision": 1},
        "enqueued": 4,
    }
    assert await operations.resolutions(settings, "tenant") == [{"decision_id": "decision"}]


@pytest.mark.asyncio
async def test_security_operations_round_trip_canonical_state(monkeypatch):
    indexing = TenantIndexingConfig(tenant_id="tenant", enabled=True)
    state = TenantIndexingState(config=indexing, epoch=2)
    snapshot = ResolvedPermissionSnapshot(
        tenant_id="tenant",
        resource_kind="document",
        resource_id="document",
        revision="acl-1",
        resolved_at=utc_now(),
        expires_at=utc_now() + timedelta(hours=1),
        known=True,
        processing_allowed=True,
        public=True,
    )
    coverage_counts = PermissionCoverageCounts(
        resources=1,
        current_snapshots=1,
        missing_snapshots=0,
        unknown_snapshots=0,
        not_yet_valid_snapshots=0,
        expired_snapshots=0,
        processing_disallowed_snapshots=0,
        public_snapshots=1,
        restricted_snapshots=0,
        coverage_complete=True,
    )
    coverage = PermissionCoverageReport(
        tenant_id="tenant",
        checked_at=utc_now(),
        sources=coverage_counts,
        documents=coverage_counts,
        corpus_present=True,
        snapshot_coverage_complete=True,
        processing_permission_complete=True,
    )
    topology = SimpleNamespace(
        configure_indexing=AsyncMock(return_value=state),
        get_indexing=AsyncMock(return_value=state),
        permission_coverage=AsyncMock(return_value=coverage),
        set_permissions=AsyncMock(),
        reconcile=AsyncMock(return_value=3),
    )
    monkeypatch.setattr(
        security_operations,
        "connect_topology_authority",
        _connect(SimpleNamespace(topology=topology)),
    )
    settings = RuntimeSettings()

    configured = await security_operations.configure_indexing(settings, indexing)
    assert configured["state"]["epoch"] == 2 and configured["enqueued"] == 3
    assert (await security_operations.indexing_status(settings, "tenant"))["epoch"] == 2
    assert (await security_operations.permission_coverage(settings, "tenant"))[
        "snapshot_coverage_complete"
    ]
    assert await security_operations.import_permissions(settings, snapshot) == {
        "resource_kind": "document",
        "resource_id": "document",
        "revision": "acl-1",
        "enqueued": 3,
    }
