from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from harborrag_core.chunking import ConnectorType, DocumentKind, RelationType
from harborrag_core.ingestion import (
    GRAPH_SCHEMA_VERSION,
    DocumentIdentityBuilder,
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId, TenantId


@dataclass(frozen=True, slots=True)
class GraphProjectionContext:
    tenant_id: TenantId
    connection_id: str
    document_id: DocumentId
    document_version_id: DocumentVersionId
    source_scope_id: str
    source_relation_version: str
    connector_type: ConnectorType
    document_kind: DocumentKind
    source_item_id: str
    source_uri: str | None


@dataclass(frozen=True, slots=True)
class GraphNodeSpec:
    kind: KnowledgeNodeKind
    entity_type: GraphEntityType
    logical_id: str
    ownership_scope: GraphOwnershipScope
    title: str | None = None
    source_scope_id: str | None = None
    document_id: DocumentId | None = None
    document_version_id: DocumentVersionId | None = None
    section_path: tuple[str, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    node_key: str | None = None


@dataclass(frozen=True, slots=True)
class GraphRelationSpec:
    relation_type: RelationType
    source: GraphNodeRecord
    target: GraphNodeRecord
    source_explicit: bool
    ownership_scope: GraphOwnershipScope | None = None
    source_scope_id: str | None = None
    document_id: DocumentId | None = None
    document_version_id: DocumentVersionId | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    source_relation_version: str | None = None


class GraphProjectionState:
    """Accumulate deterministic schema-v2 nodes and relations for one version."""

    def __init__(self, context: GraphProjectionContext) -> None:
        self.context = context
        self.nodes: dict[str, GraphNodeRecord] = {}
        self.relations: dict[str, GraphEdgeRecord] = {}
        self._identity = DocumentIdentityBuilder()

    def node(self, spec: GraphNodeSpec) -> GraphNodeRecord:
        source_scope_id = spec.source_scope_id
        if source_scope_id is None and spec.ownership_scope != GraphOwnershipScope.TENANT:
            source_scope_id = self.context.source_scope_id
        document_id = spec.document_id
        document_version_id = spec.document_version_id
        if spec.ownership_scope == GraphOwnershipScope.DOCUMENT_VERSION:
            document_id = document_id or self.context.document_id
            document_version_id = document_version_id or self.context.document_version_id
        node = GraphNodeRecord(
            node_key=spec.node_key or self._node_key(spec, document_version_id),
            node_kind=spec.kind,
            entity_type=spec.entity_type,
            logical_id=spec.logical_id,
            ownership_scope=spec.ownership_scope,
            owner_id=self.context.tenant_id,
            source_scope_id=source_scope_id,
            document_id=document_id,
            document_version_id=document_version_id,
            title=spec.title,
            section_path=spec.section_path,
            attributes=spec.attributes,
        )
        existing = self.nodes.get(node.node_key)
        if existing is not None and existing != node:
            # Prefer concrete provider metadata over a previously-created placeholder.
            if existing.attributes.get("placeholder") is True:
                self.nodes[node.node_key] = node
                return node
            if node.attributes.get("placeholder") is True:
                return existing
            raise ValueError(f"conflicting graph node projection for {node.node_key}")
        self.nodes[node.node_key] = node
        return node

    def tenant_node(self) -> GraphNodeRecord:
        tenant_id = str(self.context.tenant_id)
        return self.node(
            GraphNodeSpec(
                kind=KnowledgeNodeKind.TENANT,
                entity_type=GraphEntityType.TENANT,
                logical_id=tenant_id,
                ownership_scope=GraphOwnershipScope.TENANT,
                title=tenant_id,
                node_key=self._identity.tenant_node_key(tenant_id=tenant_id),
            )
        )

    def data_source_node(self) -> GraphNodeRecord:
        tenant_id = str(self.context.tenant_id)
        scope = self.context.source_scope_id
        return self.node(
            GraphNodeSpec(
                kind=KnowledgeNodeKind.DATA_SOURCE,
                entity_type=GraphEntityType.DATA_SOURCE,
                logical_id=scope,
                ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
                title=scope,
                attributes={
                    "connector_type": self.context.connector_type.value,
                    "connection_id": self.context.connection_id,
                },
                node_key=self._identity.data_source_node_key(
                    tenant_id=tenant_id,
                    source_scope_id=scope,
                ),
            )
        )

    def source_node(
        self,
        entity_type: GraphEntityType,
        provider_id: str,
        *,
        title: str | None = None,
        attributes: dict[str, Any] | None = None,
        source_scope_id: str | None = None,
    ) -> GraphNodeRecord:
        scope = source_scope_id or self.context.source_scope_id
        return self.node(
            GraphNodeSpec(
                kind=KnowledgeNodeKind.SOURCE_ENTITY,
                entity_type=entity_type,
                logical_id=provider_id,
                ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
                title=title,
                source_scope_id=scope,
                attributes=attributes or {},
                node_key=self._identity.source_entity_node_key(
                    tenant_id=str(self.context.tenant_id),
                    source_scope_id=scope,
                    entity_type=entity_type.value,
                    provider_id=provider_id,
                ),
            )
        )

    def document_version_node(self, *, title: str | None) -> GraphNodeRecord:
        if title and self.context.connector_type.value == "local":
            title = title.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return self.node(
            GraphNodeSpec(
                kind=KnowledgeNodeKind.DOCUMENT_VERSION,
                entity_type=GraphEntityType.DOCUMENT_VERSION,
                logical_id=str(self.context.document_id),
                ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
                title=title,
                attributes={
                    "source_item_id": self.context.source_item_id,
                    "connector_type": self.context.connector_type.value,
                    "document_kind": self.context.document_kind.value,
                    **(
                        {"source_uri": _safe_source_uri(self.context.source_uri)}
                        if self.context.source_uri
                        and self.context.connector_type.value != "local"
                        else {}
                    ),
                },
                node_key=self._identity.document_version_node_key(
                    document_id=str(self.context.document_id),
                    document_version_id=str(self.context.document_version_id),
                ),
            )
        )

    def structure_node(
        self,
        entity_type: GraphEntityType,
        logical_id: str,
        *,
        title: str | None = None,
        section_path: tuple[str, ...] = (),
        attributes: dict[str, Any] | None = None,
    ) -> GraphNodeRecord:
        return self.node(
            GraphNodeSpec(
                kind=KnowledgeNodeKind.STRUCTURE,
                entity_type=entity_type,
                logical_id=logical_id,
                ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
                title=title,
                section_path=section_path,
                attributes=attributes or {},
            )
        )

    def relation(self, spec: GraphRelationSpec) -> GraphEdgeRecord | None:
        if spec.source.node_key == spec.target.node_key:
            return None
        ownership_scope = spec.ownership_scope or self._relation_scope(spec)
        version_owned = ownership_scope == GraphOwnershipScope.DOCUMENT_VERSION
        source_scope_id = None
        document_id = None
        document_version_id = None
        if ownership_scope != GraphOwnershipScope.TENANT:
            source_scope_id = spec.source_scope_id or self.context.source_scope_id
        if version_owned:
            document_id = spec.document_id or self.context.document_id
            document_version_id = spec.document_version_id or self.context.document_version_id
        source_relation_version = (
            spec.source_relation_version or self.context.source_relation_version
            if version_owned
            else GRAPH_SCHEMA_VERSION
        )
        relation_id = self._identity.relation_id(
            relation_type=spec.relation_type,
            source_node_key=spec.source.node_key,
            target_node_key=spec.target.node_key,
            source_relation_version=source_relation_version,
        )
        relation = GraphEdgeRecord(
            relation_id=relation_id,
            relation_type=spec.relation_type,
            source_node_key=spec.source.node_key,
            target_node_key=spec.target.node_key,
            ownership_scope=ownership_scope,
            owner_id=self.context.tenant_id,
            source_scope_id=source_scope_id,
            document_id=document_id,
            document_version_id=document_version_id,
            attributes=spec.attributes,
            source_relation_version=source_relation_version,
            source_explicit=spec.source_explicit,
        )
        self.relations[relation_id] = relation
        return relation

    def _node_key(
        self,
        spec: GraphNodeSpec,
        document_version_id: DocumentVersionId | None,
    ) -> str:
        if spec.ownership_scope != GraphOwnershipScope.DOCUMENT_VERSION:
            raise ValueError("stable graph nodes require an explicit deterministic key")
        if document_version_id is None:
            raise ValueError("version-owned graph nodes require a document version")
        return self._identity.node_key(
            node_kind=spec.kind,
            entity_type=spec.entity_type.value,
            logical_id=spec.logical_id,
            document_version_id=document_version_id,
        )

    @staticmethod
    def _relation_scope(spec: GraphRelationSpec) -> GraphOwnershipScope:
        scopes = {spec.source.ownership_scope, spec.target.ownership_scope}
        if GraphOwnershipScope.DOCUMENT_VERSION in scopes:
            return GraphOwnershipScope.DOCUMENT_VERSION
        if GraphOwnershipScope.SOURCE_SCOPE in scopes:
            return GraphOwnershipScope.SOURCE_SCOPE
        return GraphOwnershipScope.TENANT


def _safe_source_uri(value: str) -> str:
    """Retain a citation identity while dropping credentials and runtime query values."""

    parsed = urlsplit(value)
    if not parsed.scheme:
        return value
    host = parsed.hostname or ""
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
