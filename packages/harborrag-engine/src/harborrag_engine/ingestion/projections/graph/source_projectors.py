"""Provider-aware stable source topology projectors for graph schema v2."""

from __future__ import annotations

from typing import Protocol

from harborrag_core.chunking import RelationType
from harborrag_core.domain.document import Document
from harborrag_core.ingestion import GraphEntityType, GraphNodeRecord

from .collaboration_source_projectors import ConfluenceSourceProjector, JiraSourceProjector
from .file_source_projectors import (
    GitHubSourceProjector,
    LocalSourceProjector,
    SharePointSourceProjector,
)
from .graph_state import GraphProjectionState
from .source_projector_support import BaseSourceProjector


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


def source_entity_type(connector_type: str) -> GraphEntityType:
    return {
        "confluence": GraphEntityType.CONFLUENCE_PAGE,
        "jira": GraphEntityType.JIRA_ISSUE,
        "github": GraphEntityType.GITHUB_FILE,
        "sharepoint": GraphEntityType.SHAREPOINT_FILE,
        "local": GraphEntityType.LOCAL_FILE,
    }.get(connector_type.casefold(), GraphEntityType.GENERIC_SOURCE_ITEM)


def relation_entity_type(
    connector_type: str,
    relation_type: RelationType,
    target_type: str,
) -> GraphEntityType:
    if relation_type == RelationType.HAS_ATTACHMENT or "attachment" in target_type.casefold():
        return {
            "confluence": GraphEntityType.CONFLUENCE_ATTACHMENT,
            "jira": GraphEntityType.JIRA_ATTACHMENT,
        }.get(connector_type.casefold(), GraphEntityType.GENERIC_SOURCE_ITEM)
    return source_entity_type(connector_type)


def source_provider_id(connector_type: str, source_item_id: str) -> str:
    """Recover the provider ID/path carried by a canonical source item identity."""

    connector = connector_type.casefold()
    value = source_item_id.strip()
    if connector in {"confluence", "sharepoint"} and "/" in value:
        return value.rstrip("/").rsplit("/", 1)[-1]
    if connector == "jira" and value.casefold().startswith("jira://"):
        return value.rstrip("/").rsplit("/", 1)[-1]
    if connector == "github" and value.casefold().startswith("github://"):
        parts = value.removeprefix("github://").split("/", 2)
        return parts[2] if len(parts) == 3 else value
    return value


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
