"""Production composition must honor registered storage substitutions."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from harborrag_adapters.repositories.errors import HarborStorageNotFoundError
from harborrag_adapters.repositories.object_store.ingestion_artifacts import (
    ARTIFACT_BUCKET,
    ImmutableArtifact,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
)
from harborrag_adapters.repositories.object_store.memory import MemoryObjectStore
from harborrag_core.contracts import HarborConflictError
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime import plugins
from harborrag_runtime.composition import resources
from harborrag_runtime.composition.storage_providers import GraphProvider, StorageProviderRegistry
from harborrag_runtime.config.settings import RuntimeSettings

pytestmark = pytest.mark.unit


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> StorageProviderRegistry:
    selected = StorageProviderRegistry()
    monkeypatch.setattr(resources, "storage_providers", selected)
    return selected


class EntryPoints(list):
    def select(self, *, group: str):
        return [entry for entry in self if entry.group == group]


def test_discovered_plugin_factories_are_used_by_production_builders(
    registry: StorageProviderRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objects = Mock(return_value=object())
    vectors = Mock(return_value=object())
    knowledge = Mock(return_value=object())
    topology = Mock(return_value=object())

    class Plugin:
        capabilities = {"storage": True}

        def register(self) -> None:
            registry.register_object_store("example", objects)
            registry.register_vector_repository("example", vectors)
            registry.register_graph("example", GraphProvider(knowledge, topology))

    entry = SimpleNamespace(group="harborrag.object_stores", name="example", load=lambda: Plugin)
    monkeypatch.setattr(plugins, "entry_points", lambda: EntryPoints([entry]))
    discovered = plugins.discover_runtime_plugins()
    settings = RuntimeSettings(
        object_store_provider="example",
        vector_provider="example",
        graph_provider="example",
    )
    assert len(discovered) == 1
    assert resources.build_object_store(settings) is objects.return_value
    assert resources.build_vector_repository(settings) is vectors.return_value
    assert resources.build_knowledge_graph(settings) is knowledge.return_value
    assert resources.build_topology_repository(settings) is topology.return_value
    for factory in (objects, vectors, knowledge, topology):
        factory.assert_called_once_with(settings)


def test_default_factories_keep_existing_provider_configuration(
    registry: StorageProviderRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objects, vectors, graph, topology = (Mock() for _ in range(4))
    monkeypatch.setattr(resources, "S3ObjectStore", objects)
    monkeypatch.setattr(resources.HarborVectorDBClient, "default", lambda: vectors)
    monkeypatch.setattr(resources, "FalkorKnowledgeGraphRepository", graph)
    monkeypatch.setattr(resources, "FalkorTopologyRepository", topology)
    settings = RuntimeSettings()
    assert settings.object_store_provider == "s3"
    assert settings.vector_provider == "qdrant"
    assert settings.graph_provider == "falkordb"
    assert resources.build_object_store(settings) is objects.return_value
    assert resources.build_vector_repository(settings) is vectors.create_from_config.return_value
    assert resources.build_knowledge_graph(settings) is graph.return_value
    assert resources.build_topology_repository(settings) is topology.return_value
    assert objects.call_args.args[0].endpoint_url == settings.object_store_endpoint_url
    assert vectors.create_from_config.call_args.args[0].url == settings.qdrant_url
    assert graph.call_args.args[0].tenant_isolation is True
    assert topology.call_args.args[0] == graph.call_args.args[0]


@pytest.mark.parametrize("family", ["object", "vector", "graph"])
def test_ambiguous_registration_is_rejected_and_same_factory_is_idempotent(
    registry: StorageProviderRegistry,
    family: str,
) -> None:
    register = {
        "object": registry.register_object_store,
        "vector": registry.register_vector_repository,
        "graph": registry.register_graph,
    }[family]
    first = GraphProvider(Mock()) if family == "graph" else Mock()
    second = GraphProvider(Mock()) if family == "graph" else Mock()
    register("custom", first)
    register("custom", first)
    with pytest.raises(ValueError, match="already registered"):
        register("custom", second)


@pytest.mark.parametrize(
    ("method", "name"),
    [
        ("register_object_store", "s3"),
        ("register_object_store", "memory"),
        ("register_object_store", "filesystem"),
        ("register_vector_repository", "qdrant"),
        ("register_graph", "falkordb"),
        ("register_object_store", " padded "),
        ("register_object_store", ""),
    ],
)
def test_reserved_or_malformed_names_are_rejected(
    registry: StorageProviderRegistry,
    method: str,
    name: str,
) -> None:
    with pytest.raises(ValueError, match="invalid or reserved"):
        getattr(registry, method)(name, Mock())


@pytest.mark.parametrize(
    ("setting", "build"),
    [
        ("object_store_provider", resources.build_object_store),
        ("vector_provider", resources.build_vector_repository),
        ("graph_provider", resources.build_knowledge_graph),
        ("graph_provider", resources.build_topology_repository),
    ],
)
def test_unknown_provider_never_silently_falls_back(
    registry: StorageProviderRegistry,
    setting: str,
    build,
) -> None:
    with pytest.raises(ValueError, match="not registered"):
        build(RuntimeSettings(**{setting: "missing-provider"}))


def test_graph_provider_can_explicitly_omit_topology(registry: StorageProviderRegistry) -> None:
    factory = Mock(return_value=object())
    registry.register_graph("structural-only", GraphProvider(factory))
    settings = RuntimeSettings(graph_provider="structural-only")
    assert resources.build_knowledge_graph(settings) is factory.return_value
    with pytest.raises(ValueError, match="no topology support"):
        resources.build_topology_repository(settings)
    factory.assert_called_once_with(settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["memory", "filesystem", "plugin-memory"])
async def test_immutable_artifacts_round_trip_through_selected_provider(
    registry: StorageProviderRegistry,
    tmp_path: Path,
    provider: str,
) -> None:
    factory = Mock(side_effect=lambda settings: MemoryObjectStore())
    registry.register_object_store("plugin-memory", factory)
    settings = RuntimeSettings(
        object_store_provider=provider, object_store_root=tmp_path / "objects"
    )
    store = resources.build_object_store(settings)
    await store.connect()
    try:
        await store.ensure_buckets((ARTIFACT_BUCKET,))
        context = StorageOperationContext.system("tenant-a")
        artifact = ImmutableArtifact(
            bucket=ARTIFACT_BUCKET,
            key="canonical/document/version.json",
            payload=b'{"text":"stored evidence"}',
            media_type="application/json",
            artifact_kind="canonical",
        )
        writer, reader = ImmutableArtifactWriter(store), ImmutableArtifactReader(store)
        reference = await writer.put(artifact, context=context)
        assert await reader.get(reference, context=context) == artifact.payload
        assert await writer.put(artifact, context=context) == reference
        with pytest.raises(HarborConflictError, match="different content"):
            await writer.put(replace(artifact, payload=b"changed"), context=context)
        with pytest.raises(HarborStorageNotFoundError):
            await reader.get(reference, context=StorageOperationContext.system("tenant-b"))
        if provider == "filesystem":
            reopened = resources.build_object_store(settings)
            await reopened.connect()
            try:
                assert (
                    await ImmutableArtifactReader(reopened).get(reference, context=context)
                    == artifact.payload
                )
            finally:
                await reopened.close()
        elif provider == "plugin-memory":
            factory.assert_called_once_with(settings)
    finally:
        await store.close()
