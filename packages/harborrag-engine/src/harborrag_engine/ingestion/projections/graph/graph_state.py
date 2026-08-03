from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.chunking import ConnectorType, DocumentKind, RelationType
from harborrag_core.ingestion import (
    DocumentIdentityBuilder,
    GraphEdgeRecord,
    GraphNodeRecord,
    KnowledgeNodeKind,
)
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId


@dataclass(frozen=True, slots=True)
class GraphProjectionContext:
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
    logical_id: str
    document_id: DocumentId
    document_version_id: DocumentVersionId
    source_scope_id: str
    title: str | None = None
    connector_type: ConnectorType | None = None
    document_kind: DocumentKind | None = None
    source_item_id: str | None = None
    source_uri: str | None = None
    content_preview: str | None = None
    section_path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphRelationSpec:
    relation_type: RelationType
    source: GraphNodeRecord
    target: GraphNodeRecord
    source_explicit: bool
    evidence_chunk_ids: tuple[str, ...] = ()
    source_relation_version: str | None = None
    document_version_id: DocumentVersionId | None = None


class GraphProjectionState:
    """Accumulate deterministic graph nodes and relations for one version."""

    def __init__(self, context: GraphProjectionContext) -> None:
        self.context = context
        self.nodes: dict[str, GraphNodeRecord] = {}
        self.relations: dict[str, GraphEdgeRecord] = {}
        self._identity = DocumentIdentityBuilder()

    def node(self, spec: GraphNodeSpec) -> GraphNodeRecord:
        node = GraphNodeRecord(
            node_key=self._identity.node_key(
                node_kind=spec.kind,
                logical_id=spec.logical_id,
                document_version_id=spec.document_version_id,
            ),
            node_kind=spec.kind,
            logical_id=spec.logical_id,
            document_id=spec.document_id,
            document_version_id=spec.document_version_id,
            source_scope_id=spec.source_scope_id,
            title=spec.title,
            connector_type=spec.connector_type,
            document_kind=spec.document_kind,
            source_item_id=spec.source_item_id,
            source_uri=spec.source_uri,
            content_preview=spec.content_preview,
            section_path=spec.section_path,
        )
        self.nodes[node.node_key] = node
        return node

    def current_node(
        self,
        kind: KnowledgeNodeKind,
        logical_id: str,
        *,
        title: str | None = None,
        content_preview: str | None = None,
        section_path: tuple[str, ...] = (),
    ) -> GraphNodeRecord:
        return self.node(
            GraphNodeSpec(
                kind=kind,
                logical_id=logical_id,
                document_id=self.context.document_id,
                document_version_id=self.context.document_version_id,
                source_scope_id=self.context.source_scope_id,
                title=title,
                connector_type=self.context.connector_type,
                document_kind=self.context.document_kind,
                source_item_id=self.context.source_item_id,
                source_uri=self.context.source_uri,
                content_preview=content_preview,
                section_path=section_path,
            )
        )

    def relation(self, spec: GraphRelationSpec) -> GraphEdgeRecord | None:
        if spec.source.node_key == spec.target.node_key:
            return None
        source_relation_version = (
            spec.source_relation_version or self.context.source_relation_version
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
            document_version_id=(spec.document_version_id or self.context.document_version_id),
            source_relation_version=source_relation_version,
            source_explicit=spec.source_explicit,
            evidence_chunk_ids=tuple(dict.fromkeys(spec.evidence_chunk_ids)),
        )
        self.relations[relation_id] = relation
        return relation
