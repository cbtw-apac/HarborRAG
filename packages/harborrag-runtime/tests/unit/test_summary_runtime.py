"""Runtime wiring for independently scheduled summary projection."""

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
from harborrag_runtime.topology import derived_factory, summary_factory
from harborrag_runtime.topology.derived_factory import DerivedRuntimeFactory
from harborrag_runtime.topology.summary_factory import SummaryRuntimeFactory
from harborrag_runtime.topology.summary_inputs import SummaryInputLoader
from harborrag_runtime.topology.summary_policy import build_summary_policy


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
