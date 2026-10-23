"""Explicit storage factory registration consumed by production composition.

Entry-point plugins register factories here from their ``register()`` method.
Registration never opens connections; factories receive the active runtime settings
and return a new resource whose lifecycle is owned by the calling runtime.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from harborrag_core.ports.indexing import KnowledgeGraphRepositoryPort
from harborrag_core.ports.retrieval import GraphRetrievalRepositoryPort
from harborrag_core.ports.storage import ObjectStorePort, VectorRepositoryPort
from harborrag_core.ports.topology_projection import TopologyProjectionPort
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import DocumentTopologyBuild
from harborrag_core.topology.derived import ParentDescription

from ..config.settings import RuntimeSettings


class RuntimeKnowledgeGraphPort(
    KnowledgeGraphRepositoryPort, GraphRetrievalRepositoryPort, Protocol
):
    """Structural graph write and retrieval operations used by runtime."""


class RuntimeTopologyPort(TopologyProjectionPort, Protocol):
    async def connect(self, *, provision: bool = True) -> None: ...

    async def close(self) -> None: ...

    async def write_parents(
        self,
        build: DocumentTopologyBuild,
        parents: tuple[ParentDescription, ...],
        *,
        context: StorageOperationContext,
    ) -> None: ...

    async def verify_parents(
        self,
        build: DocumentTopologyBuild,
        parents: tuple[ParentDescription, ...],
        *,
        context: StorageOperationContext,
    ) -> bool: ...


type ObjectStoreFactory = Callable[[RuntimeSettings], ObjectStorePort]
type VectorRepositoryFactory = Callable[[RuntimeSettings], VectorRepositoryPort]
type KnowledgeGraphFactory = Callable[[RuntimeSettings], RuntimeKnowledgeGraphPort]
type TopologyFactory = Callable[[RuntimeSettings], RuntimeTopologyPort]


@dataclass(frozen=True)
class GraphProvider:
    knowledge: KnowledgeGraphFactory
    topology: TopologyFactory | None = None


class StorageProviderRegistry:
    """Reject ambiguous registration and fail clearly for unconfigured providers."""

    def __init__(self) -> None:
        self.objects: dict[str, ObjectStoreFactory] = {}
        self.vectors: dict[str, VectorRepositoryFactory] = {}
        self.graphs: dict[str, GraphProvider] = {}

    def register_object_store(self, name: str, factory: ObjectStoreFactory) -> None:
        _register(self.objects, name, factory, reserved={"s3", "memory", "filesystem"})

    def register_vector_repository(self, name: str, factory: VectorRepositoryFactory) -> None:
        _register(self.vectors, name, factory, reserved={"qdrant"})

    def register_graph(self, name: str, provider: GraphProvider) -> None:
        _register(self.graphs, name, provider, reserved={"falkordb"})

    def object_store(self, settings: RuntimeSettings) -> ObjectStorePort:
        return _resolve(self.objects, settings.object_store_provider)(settings)

    def vector_repository(self, settings: RuntimeSettings) -> VectorRepositoryPort:
        return _resolve(self.vectors, settings.vector_provider)(settings)

    def knowledge_graph(self, settings: RuntimeSettings) -> RuntimeKnowledgeGraphPort:
        return _resolve(self.graphs, settings.graph_provider).knowledge(settings)

    def topology(self, settings: RuntimeSettings) -> RuntimeTopologyPort:
        provider = _resolve(self.graphs, settings.graph_provider)
        if provider.topology is None:
            raise ValueError(f"graph provider {settings.graph_provider!r} has no topology support")
        return provider.topology(settings)


def _register[T](registry: dict[str, T], name: str, value: T, *, reserved: set[str]) -> None:
    if not name or name.strip() != name or name in reserved:
        raise ValueError(f"invalid or reserved storage provider name: {name!r}")
    if name in registry and registry[name] != value:
        raise ValueError(f"storage provider {name!r} is already registered")
    registry[name] = value


def _resolve[T](registry: dict[str, T], name: str) -> T:
    try:
        return registry[name]
    except KeyError:
        raise ValueError(
            f"storage provider {name!r} is not registered; load its plugin before composition"
        ) from None


storage_providers = StorageProviderRegistry()
