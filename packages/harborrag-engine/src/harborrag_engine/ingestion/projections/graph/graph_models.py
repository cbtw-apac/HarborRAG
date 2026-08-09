from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256

from harborrag_core.chunking import ChunkRecord
from harborrag_core.domain.document import Document
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphNodeRecord,
    GraphOwnershipScope,
    GraphProjectionManifest,
)
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId


@dataclass(frozen=True, slots=True)
class GraphDocumentTarget:
    """A resolved published document endpoint for a source relation."""

    source_item_id: str
    document_id: DocumentId
    document_version_id: DocumentVersionId
    source_scope_id: str
    title: str | None = None

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.source_item_id,
                self.document_id,
                self.document_version_id,
                self.source_scope_id,
            )
        ):
            raise ValueError("graph document target identity values must be non-empty")


@dataclass(frozen=True, slots=True)
class UnresolvedGraphRelation:
    relation_type: str
    target_source_item_id: str


@dataclass(frozen=True, slots=True)
class GraphProjectionInput:
    document: Document
    chunks: tuple[ChunkRecord, ...]
    resolved_targets: Mapping[str, GraphDocumentTarget]
    graph_projection_version: str

    def __post_init__(self) -> None:
        if not self.chunks:
            raise ValueError("graph projection requires at least one canonical chunk")
        if not self.graph_projection_version.strip():
            raise ValueError("graph_projection_version must be non-empty")
        document_ids = {str(chunk.document_id) for chunk in self.chunks}
        version_ids = {str(chunk.document_version_id) for chunk in self.chunks}
        scopes = {chunk.source_scope_id for chunk in self.chunks}
        tenants = {str(chunk.tenant_id) for chunk in self.chunks}
        if len(document_ids) != 1 or len(version_ids) != 1 or len(scopes) != 1 or len(tenants) != 1:
            raise ValueError("graph projection chunks must belong to one document version")
        if document_ids != {self.document.id}:
            raise ValueError("graph projection document must match its canonical chunks")


@dataclass(frozen=True, slots=True)
class GraphProjectionBatch:
    nodes: tuple[GraphNodeRecord, ...]
    relations: tuple[GraphEdgeRecord, ...]
    unresolved_relations: tuple[UnresolvedGraphRelation, ...] = ()
    manifest: GraphProjectionManifest = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "nodes",
            tuple(sorted(self.nodes, key=lambda node: node.node_key)),
        )
        object.__setattr__(
            self,
            "relations",
            tuple(sorted(self.relations, key=lambda relation: relation.relation_id)),
        )
        node_keys = {node.node_key for node in self.nodes}
        relation_ids = {relation.relation_id for relation in self.relations}
        if len(node_keys) != len(self.nodes):
            raise ValueError("graph projection node keys must be unique")
        if len(relation_ids) != len(self.relations):
            raise ValueError("graph projection relation IDs must be unique")
        if any(
            relation.source_node_key not in node_keys or relation.target_node_key not in node_keys
            for relation in self.relations
        ):
            raise ValueError("every graph relation endpoint must exist in the batch")
        nodes_by_key = {node.node_key: node for node in self.nodes}
        for relation in self.relations:
            endpoints = (
                nodes_by_key[relation.source_node_key],
                nodes_by_key[relation.target_node_key],
            )
            if any(node.owner_id != relation.owner_id for node in endpoints):
                raise ValueError("graph relation owner must match both endpoints")
            if relation.ownership_scope == GraphOwnershipScope.DOCUMENT_VERSION:
                version_endpoints = tuple(
                    node
                    for node in endpoints
                    if node.ownership_scope == GraphOwnershipScope.DOCUMENT_VERSION
                )
                if not version_endpoints or any(
                    node.document_id != relation.document_id
                    or node.document_version_id != relation.document_version_id
                    for node in version_endpoints
                ):
                    raise ValueError("version-owned relation must match its version endpoint")
            elif any(
                node.ownership_scope == GraphOwnershipScope.DOCUMENT_VERSION for node in endpoints
            ):
                raise ValueError("stable relationship may not own a version endpoint")
        object.__setattr__(self, "manifest", self._build_manifest())

    def _build_manifest(self) -> GraphProjectionManifest:
        relation_versions = {
            relation.document_version_id
            for relation in self.relations
            if relation.ownership_scope == GraphOwnershipScope.DOCUMENT_VERSION
            and relation.document_version_id is not None
        }
        if len(relation_versions) > 1:
            raise ValueError("graph relations must belong to one projected document version")
        document_version_id = (
            next(iter(relation_versions)) if relation_versions else self._single_node_version()
        )
        current_nodes = tuple(
            node
            for node in self.nodes
            if node.ownership_scope == GraphOwnershipScope.DOCUMENT_VERSION
            and node.document_version_id == document_version_id
        )
        document_ids = {node.document_id for node in current_nodes if node.document_id is not None}
        if len(document_ids) != 1:
            raise ValueError("graph projection cannot identify its owning document")
        records = [
            *(
                {"record_type": "node", "value": node.model_dump(mode="json")}
                for node in self.nodes
            ),
            *(
                {"record_type": "edge", "value": edge.model_dump(mode="json")}
                for edge in self.relations
            ),
        ]
        payload = json.dumps(
            records,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return GraphProjectionManifest(
            document_id=next(iter(document_ids)),
            document_version_id=document_version_id,
            node_keys=tuple(node.node_key for node in self.nodes),
            relation_ids=tuple(relation.relation_id for relation in self.relations),
            payload_sha256=sha256(payload).hexdigest(),
        )

    def _single_node_version(self) -> DocumentVersionId:
        versions = {
            node.document_version_id
            for node in self.nodes
            if node.ownership_scope == GraphOwnershipScope.DOCUMENT_VERSION
            and node.document_version_id is not None
        }
        if len(versions) != 1:
            raise ValueError("relation-free graph projection must belong to one document version")
        return next(iter(versions))
