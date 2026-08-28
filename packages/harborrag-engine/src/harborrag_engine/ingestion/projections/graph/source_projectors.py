"""Provider-aware stable source topology projectors for graph schema v2."""

from __future__ import annotations

from typing import Protocol

from harborrag_core.chunking import RelationType
from harborrag_core.domain.document import Document
from harborrag_core.ingestion import GraphNodeRecord

from .collaboration_source_projectors import ConfluenceSourceProjector, JiraSourceProjector
from .file_source_projectors import (
    GitHubSourceProjector,
    LocalSourceProjector,
    SharePointSourceProjector,
)
from .graph_state import GraphProjectionState
from .source_projector_support import (
    BaseSourceProjector,
    relation_entity_type,
    source_entity_type,
    source_provider_id,
)


class GraphSourceProjector(Protocol):
    """Build stable provider hierarchy from canonical provenance metadata."""

    def project(
        self,
        state: GraphProjectionState,
        document: Document,
        data_source: GraphNodeRecord,
        document_version: GraphNodeRecord,
    ) -> GraphNodeRecord: ...


class GraphSourceProjectorRegistry:
    """Resolve provider projectors without leaking providers into core contracts."""

    def __init__(self) -> None:
        self._projectors: dict[str, GraphSourceProjector] = {}

    def register(self, connector_type: str, projector: GraphSourceProjector) -> None:
        key = connector_type.strip().casefold()
        if not key:
            raise ValueError("graph source projector connector type must be non-empty")
        self._projectors[key] = projector

    def resolve(self, connector_type: str) -> GraphSourceProjector:
        return self._projectors.get(connector_type.casefold(), GenericSourceProjector())

    def connector_types(self) -> frozenset[str]:
        """Registered connector-type keys, as ``register`` casefolded them."""

        return frozenset(self._projectors)


def default_graph_source_projector_registry() -> GraphSourceProjectorRegistry:
    registry = GraphSourceProjectorRegistry()
    registry.register("confluence", ConfluenceSourceProjector())
    registry.register("jira", JiraSourceProjector())
    registry.register("github", GitHubSourceProjector())
    registry.register("sharepoint", SharePointSourceProjector())
    registry.register("local", LocalSourceProjector())
    return registry


class GenericSourceProjector(BaseSourceProjector):
    def project(
        self,
        state: GraphProjectionState,
        document: Document,
        data_source: GraphNodeRecord,
        document_version: GraphNodeRecord,
    ) -> GraphNodeRecord:
        item = self.source_item(state, document)
        self.edge(state, RelationType.CONTAINS, data_source, item)
        self.version(state, item, document_version)
        return item


__all__ = [
    "ConfluenceSourceProjector",
    "GenericSourceProjector",
    "GitHubSourceProjector",
    "GraphSourceProjector",
    "GraphSourceProjectorRegistry",
    "JiraSourceProjector",
    "LocalSourceProjector",
    "SharePointSourceProjector",
    "default_graph_source_projector_registry",
    "relation_entity_type",
    "source_entity_type",
    "source_provider_id",
]
