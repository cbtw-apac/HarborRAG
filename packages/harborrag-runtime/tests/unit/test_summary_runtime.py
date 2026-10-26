"""Runtime wiring for independently scheduled summary projection."""

from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from harborrag_core.base import utc_now
from harborrag_core.chunking import RecordKind
from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
    ProjectionManifest,
)
from harborrag_core.summaries import SummaryLease, SummaryPolicy, SummarySnapshot
from harborrag_runtime.config.graph_build import (
    GraphBuildConfig,
    GraphBuildSourceConfig,
    GraphBuildTenantConfig,
)
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.topology import derived_factory, summary_factory, summary_operations
from harborrag_runtime.topology.derived_factory import DerivedRuntimeFactory
from harborrag_runtime.topology.summary_factory import SummaryRuntimeFactory
from harborrag_runtime.topology.summary_inputs import SummaryInputLoader
from harborrag_runtime.topology.summary_policy import build_summary_policy


def _authority(control):
    @asynccontextmanager
    async def connect(*_args, **_kwargs):
        yield control

    return connect


@pytest.mark.asyncio
async def test_summary_factory_initializes_and_synchronizes_managed_policy(monkeypatch):
    tenant = GraphBuildTenantConfig(
        tenant_id="managed",
        mode="llm",
        sources=[
            GraphBuildSourceConfig(source_scope_id="allowed"),
            GraphBuildSourceConfig(source_scope_id="disabled", enabled=False),
        ],
    )
    config = GraphBuildConfig(tenants=[tenant])
    monkeypatch.setattr(summary_factory.GraphBuildConfig, "from_settings", lambda _settings: config)
    policy = SummaryPolicy(
        model_fingerprint="model",
        tenant_enabled=True,
    )
    monkeypatch.setattr(summary_factory, "build_summary_policy", lambda *_args: policy)
    summaries = SimpleNamespace(
        source_scope_ids=AsyncMock(return_value=("discovered", "allowed")),
        configure=AsyncMock(),
    )
    topology = SimpleNamespace(configure_indexing=AsyncMock())
    factory = SummaryRuntimeFactory(
        RuntimeSettings(topology_parent_enabled=True),
        SimpleNamespace(summaries=summaries, topology=topology),
        Mock(),
        Mock(),
    )

    assert factory.policy() is policy
    await factory.initialize("managed")
    await factory.initialize("unknown")
    configured = topology.configure_indexing.await_args.args[0]
    assert configured.tenant_id == "managed" and configured.enabled

    await factory.synchronize("managed")
    calls = [call.args for call in summaries.configure.await_args_list]
    assert calls == [
        ("managed", "allowed", policy),
        ("managed", "disabled", None),
        ("managed", "discovered", None),
        ("managed", "@tenant", policy),
    ]


@pytest.mark.asyncio
async def test_summary_factory_synchronizes_discovered_scopes_without_yaml_tenant(monkeypatch):
    monkeypatch.setattr(
        summary_factory.GraphBuildConfig,
        "from_settings",
        lambda _settings: GraphBuildConfig(),
    )
    policy = SummaryPolicy(model_fingerprint="model")
    monkeypatch.setattr(summary_factory, "build_summary_policy", lambda *_args: policy)
    summaries = SimpleNamespace(
        source_scope_ids=AsyncMock(return_value=("discovered",)),
        configure=AsyncMock(),
    )
    factory = SummaryRuntimeFactory(
        RuntimeSettings(topology_parent_enabled=True),
        SimpleNamespace(summaries=summaries),
        Mock(),
        Mock(),
    )

    await factory.synchronize("unmanaged")

    assert [call.args for call in summaries.configure.await_args_list] == [
        ("unmanaged", "discovered", policy),
        ("unmanaged", "@tenant", None),
    ]


def test_summary_factory_pins_generator_policy_and_composes_service(monkeypatch):
    settings = RuntimeSettings(topology_parent_model="summary-model")
    factory = SummaryRuntimeFactory(settings, Mock(), Mock(), Mock())
    policy = SummaryPolicy(model_fingerprint="model")
    lease = SummaryLease(
        tenant_id="tenant",
        source_scope_id="scope",
        revision=1,
        fence=2,
        policy=policy,
        lease_until=utc_now() + timedelta(minutes=1),
    )
    catalog = Mock()
    monkeypatch.setattr(summary_factory.HarborChatClientConfig, "from_file", lambda _path: catalog)
    monkeypatch.setattr(summary_factory, "build_summary_policy", lambda *_args: policy)
    monkeypatch.setattr(
        summary_factory,
        "default_extraction_profile",
        lambda *_args: SimpleNamespace(model="pinned-model"),
    )
    generator = Mock()
    configured = Mock(return_value=generator)
    monkeypatch.setattr(summary_factory, "ConfiguredDescriptionGenerator", configured)

    assert factory.generator(lease) is generator
    assert configured.call_args.args[2:] == ("tenant", "summary:scope")
    assert configured.call_args.kwargs == {"frozen_catalog": catalog}
    service = factory.service()
    assert service.repository is factory.control.summaries
    assert service.budget is factory.control.topology

    monkeypatch.setattr(
        summary_factory,
        "build_summary_policy",
        lambda *_args: policy.model_copy(update={"model_fingerprint": "changed"}),
    )
    with pytest.raises(ValueError, match="configuration changed"):
        factory.generator(lease)


@pytest.mark.asyncio
async def test_connect_summaries_builds_store_and_initializes_factory(monkeypatch):
    settings = RuntimeSettings()
    effective = settings.model_copy(update={"summary_task_queue": "effective"})
    config = Mock()
    config.effective_settings.return_value = effective
    monkeypatch.setattr(
        summary_operations.GraphBuildConfig, "from_settings", lambda _settings: config
    )
    control = Mock()
    monkeypatch.setattr(summary_operations, "connect_topology_authority", _authority(control))
    store = SimpleNamespace(
        connect=AsyncMock(),
        ensure_buckets=AsyncMock(),
        close=AsyncMock(),
    )
    monkeypatch.setattr(summary_operations, "build_object_store", lambda _settings: store)
    factory = SimpleNamespace(initialize=AsyncMock())
    constructor = Mock(return_value=factory)
    monkeypatch.setattr(summary_operations, "SummaryRuntimeFactory", constructor)

    async with summary_operations.connect_summaries(settings, "tenant") as connected:
        assert connected is factory

    store.connect.assert_awaited_once()
    store.ensure_buckets.assert_awaited_once_with((summary_operations.ARTIFACT_BUCKET,))
    store.close.assert_awaited_once()
    factory.initialize.assert_awaited_once_with("tenant")
    assert constructor.call_args.args[0:2] == (effective, control)


@pytest.mark.asyncio
async def test_summary_administration_operations_delegate_to_composed_ports(monkeypatch):
    settings = RuntimeSettings()
    summaries = SimpleNamespace(
        status=AsyncMock(return_value=("ready",)),
        backfill=AsyncMock(return_value=3),
        reconcile=AsyncMock(),
        cleanup=AsyncMock(return_value={"removed": 2}),
    )
    control = SimpleNamespace(summaries=summaries)
    monkeypatch.setattr(summary_operations, "connect_topology_authority", _authority(control))
    service = SimpleNamespace(run_once=AsyncMock(return_value="current"))
    factory = SimpleNamespace(
        settings=settings,
        control=control,
        synchronize=AsyncMock(),
        service=Mock(return_value=service),
    )

    @asynccontextmanager
    async def connect(_settings, _tenant_id):
        yield factory

    monkeypatch.setattr(summary_operations, "connect_summaries", connect)
    temporal = object()
    monkeypatch.setattr(
        summary_operations, "connect_temporal_client", AsyncMock(return_value=temporal)
    )
    watch = AsyncMock()
    monkeypatch.setattr(summary_operations, "watch_summaries", watch)

    assert await summary_operations.status(settings, "tenant") == {
        "scopes": ("ready",),
        "limit": 100,
    }
    assert await summary_operations.backfill(settings, "tenant", "scope") == {"enqueued": 3}
    assert await summary_operations.run_once(settings, "tenant") == {"state": "current"}
    await summary_operations.worker(settings, "tenant")
    assert await summary_operations.cleanup(settings, "tenant", retention_days=7, apply=True) == {
        "removed": 2
    }

    assert factory.synchronize.await_count == 2
    summaries.reconcile.assert_awaited_once_with("tenant")
    watch.assert_awaited_once_with(temporal, factory, "tenant")
    summaries.cleanup.assert_awaited_once_with("tenant", retention_days=7, apply=True)


@pytest.mark.asyncio
async def test_summary_temporal_adapters_are_loaded_on_demand(monkeypatch):
    from harborrag_runtime.temporal import connection
    from harborrag_runtime.topology import summary_worker

    config = Mock()
    client = object()
    connect = AsyncMock(return_value=client)
    watch = AsyncMock()
    monkeypatch.setattr(connection, "connect_temporal_client", connect)
    monkeypatch.setattr(summary_worker, "watch_summaries", watch)
    factory = Mock()

    assert await summary_operations.connect_temporal_client(config) is client
    await summary_operations.watch_summaries(client, factory, "tenant")

    connect.assert_awaited_once_with(config)
    watch.assert_awaited_once_with(client, factory, "tenant")


def test_summary_policy_loads_catalog_only_when_not_supplied(monkeypatch):
    settings = RuntimeSettings()
    catalog = object()
    loaded = Mock(return_value=catalog)
    monkeypatch.setattr(summary_factory.HarborChatClientConfig, "from_file", loaded)
    profile = SimpleNamespace(
        model="model",
        deployment_revision="deployment",
        temperature=None,
        reasoning_effort=None,
    )
    monkeypatch.setattr(
        "harborrag_runtime.topology.summary_policy.default_extraction_profile",
        lambda *_args: profile,
    )

    explicit = build_summary_policy(settings, catalog)
    assert not loaded.called
    implicit = build_summary_policy(settings)
    assert implicit == explicit
    loaded.assert_called_once_with(settings.model_config_path)


@pytest.mark.asyncio
async def test_derived_factory_opens_resources_lazily_for_cleanup(monkeypatch):
    vectors = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(derived_factory, "build_vector_repository", lambda _settings: vectors)
    cleaner = SimpleNamespace(delete=AsyncMock(return_value=2))
    monkeypatch.setattr(
        derived_factory, "RetiredDerivedProjectionCleaner", Mock(return_value=cleaner)
    )
    factory = DerivedRuntimeFactory(RuntimeSettings(), Mock(), Mock(), Mock(), Mock())
    context = Mock()

    assert await factory.cleanup("build", (), context=context) == 0
    records = (Mock(),)
    assert await factory.cleanup("build", records, context=context) == 2
    vectors.connect.assert_awaited_once()
    vectors.close.assert_awaited_once()
    cleaner.delete.assert_awaited_once_with("build", records, context=context)


@pytest.mark.asyncio
async def test_derived_factory_composes_enabled_enrichment(monkeypatch):
    settings = RuntimeSettings(
        topology_derived_enabled=True,
        topology_parent_enabled=True,
    )
    profile = SimpleNamespace(fingerprint="vectors", description_revision="descriptions")
    build = SimpleNamespace(
        job_id="job",
        chunk_ids=("chunk",),
        document_id="document",
    )
    extraction_profile = SimpleNamespace(fingerprint="extraction")
    job = SimpleNamespace(
        document_version_id="version",
        document_id="document",
        policy=SimpleNamespace(profile=extraction_profile),
    )
    repository = SimpleNamespace(
        get_build=AsyncMock(return_value=build),
        get_build_lineage=AsyncMock(return_value=object()),
        get_job=AsyncMock(return_value=job),
    )
    snapshot = SimpleNamespace(chunk_artifact=object())
    control = SimpleNamespace(
        topology=repository,
        document_versions=SimpleNamespace(get_version=AsyncMock(return_value=snapshot)),
    )
    factory = DerivedRuntimeFactory(settings, control, Mock(), Mock(), Mock())
    chunk = SimpleNamespace(record_kind=RecordKind.EVIDENCE, content="content")

    class Chunks:
        def __init__(self, _reader):
            pass

        async def get_all(self, *_args, **_kwargs):
            return (chunk,)

    monkeypatch.setattr(derived_factory, "ChunkArtifactReader", Chunks)
    monkeypatch.setattr(
        derived_factory,
        "extraction_input",
        lambda *_args, **_kwargs: SimpleNamespace(chunk_id="chunk"),
    )
    vectors = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(derived_factory, "build_vector_repository", lambda _settings: vectors)
    retry = Mock()
    retry.model_copy.return_value = object()
    embed_config = Mock(retry=retry)
    embed_config.model_copy.return_value = object()
    monkeypatch.setattr(
        derived_factory.HarborEmbedClientConfig,
        "from_file",
        lambda _path: embed_config,
    )
    monkeypatch.setattr(derived_factory, "build_contextual_profile", lambda *_args: profile)
    telemetry = Mock()
    monkeypatch.setattr(
        derived_factory, "build_model_telemetry", lambda *_args, **_kwargs: telemetry
    )
    embed = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(
        derived_factory.HarborEmbedClient,
        "from_config",
        Mock(return_value=embed),
    )
    coordinator = SimpleNamespace(complete=AsyncMock(return_value={"contextual": "ready"}))
    coordinator_type = Mock(return_value=coordinator)
    monkeypatch.setattr(derived_factory, "DerivedEnrichmentCoordinator", coordinator_type)

    assert await factory.complete("tenant", "build") == {"contextual": "ready"}
    vectors.connect.assert_awaited_once()
    vectors.close.assert_awaited_once()
    embed.aclose.assert_awaited_once()
    coordinator.complete.assert_awaited_once_with(job, build, (SimpleNamespace(chunk_id="chunk"),))
    resources = coordinator_type.call_args.args[0]
    assert resources.parent_loader is not None


@pytest.mark.asyncio
async def test_derived_factory_rejects_missing_canonical_inputs():
    disabled = DerivedRuntimeFactory(RuntimeSettings(), Mock(), Mock(), Mock(), Mock())
    assert await disabled.complete("tenant", "build") == {
        "contextual": "disabled",
        "parents": "disabled",
    }

    settings = RuntimeSettings(topology_derived_enabled=True)
    repository = SimpleNamespace(
        get_build=AsyncMock(return_value=None),
        get_build_lineage=AsyncMock(return_value=object()),
    )
    control = SimpleNamespace(topology=repository)
    factory = DerivedRuntimeFactory(settings, control, Mock(), Mock(), Mock())
    with pytest.raises(ValueError, match="current accepted build"):
        await factory.complete("tenant", "build")

    build = SimpleNamespace(job_id="job")
    repository.get_build.return_value = build
    repository.get_job = AsyncMock(return_value=None)
    with pytest.raises(ValueError, match="canonical job"):
        await factory.complete("tenant", "build")

    repository.get_job.return_value = SimpleNamespace(document_version_id="version")
    control.document_versions = SimpleNamespace(get_version=AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="frozen source chunks"):
        await factory.complete("tenant", "build")


def _summary_lease() -> SummaryLease:
    return SummaryLease(
        tenant_id="tenant",
        source_scope_id="scope",
        revision=1,
        fence=1,
        policy=SummaryPolicy(model_fingerprint="model"),
        lease_until=utc_now() + timedelta(minutes=1),
    )


@pytest.mark.asyncio
async def test_summary_input_loader_builds_empty_source_and_rejects_changed_manifests():
    repository = SimpleNamespace(
        source_documents=AsyncMock(return_value=[]),
        retained_nodes=AsyncMock(return_value=()),
    )
    loader = SummaryInputLoader(
        repository,
        Mock(),
        Mock(),
        RuntimeSettings(),
    )
    lease = _summary_lease()
    empty = SummarySnapshot(
        tenant_id="tenant",
        source_scope_id="scope",
        document_versions={},
        permission_dependencies=(),
        membership_digest="empty",
    )

    plan = await loader.load(lease, empty)
    assert len(plan) == 1
    assert plan[0].kind == "DataSource" and not plan[0].input_chunk_ids

    repository.source_documents.return_value = [
        {
            "document_id": "document",
            "active_document_version_id": "version",
            "manifest": ProjectionManifest(
                document_id="document", document_version_id="version"
            ).model_dump(mode="json"),
            "chunk_artifact": None,
        }
    ]
    with pytest.raises(HarborConflictError, match="inputs changed"):
        await loader.load(lease, empty)

    current = empty.model_copy(update={"document_versions": {"document": "version"}})
    with pytest.raises(ValueError, match="manifest unavailable"):
        await loader.load(lease, current)


def test_summary_input_loader_merges_only_stable_in_scope_nodes():
    lease = _summary_lease()

    def node(owner="tenant", scope="scope", title="Source"):
        return GraphNodeRecord(
            node_key="source",
            node_kind=KnowledgeNodeKind.DATA_SOURCE,
            entity_type=GraphEntityType.DATA_SOURCE,
            logical_id="scope",
            ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
            owner_id=owner,
            source_scope_id=scope,
            title=title,
        )

    with pytest.raises(ValueError, match="ownership"):
        SummaryInputLoader._merge_nodes({}, (node(owner="other"),), lease)

    merged = {}
    SummaryInputLoader._merge_nodes(merged, (node(scope="other"),), lease)
    assert not merged
    SummaryInputLoader._merge_nodes(merged, (node(title="Zulu"), node(title="Alpha")), lease)
    assert merged["source"].title in {"Alpha", "Zulu"}
