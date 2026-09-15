"""Canonical graph retrieval contracts without provider terminology."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import GraphEdgeRecord, GraphNodeRecord


class GraphDirection(StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"
    BOTH = "both"


class GraphNodeSelectorKind(StrEnum):
    NODE_KEY = "node_key"
    PROVIDER_ID = "provider_id"
    EXACT_TITLE = "exact_title"


class GraphNodeResolutionQuery(StrictModel):
    """Resolve an exact portable selector to bounded graph-node candidates."""

    selector_kind: GraphNodeSelectorKind
    value: str = Field(min_length=1, max_length=512)
    source_scope_ids: tuple[str, ...] = Field(default=(), max_length=10)
    entity_types: tuple[str, ...] = Field(default=(), max_length=10)
    # Public surfaces cap this at ten. The wider core bound lets the runtime over-fetch
    # before permission filtering so denied title collisions cannot starve visible ones.
    limit: int = Field(default=5, ge=1, le=100)

    @model_validator(mode="after")
    def validate_scopes(self) -> Self:
        if any(not item.strip() for item in (*self.source_scope_ids, *self.entity_types)):
            raise ValueError("graph resolver scopes must be non-empty")
        if len(set(self.source_scope_ids)) != len(self.source_scope_ids):
            raise ValueError("source_scope_ids must be unique")
        if len(set(self.entity_types)) != len(self.entity_types):
            raise ValueError("entity_types must be unique")
        return self


class GraphNodeResolutionResult(StrictModel):
    candidates: tuple[GraphNodeRecord, ...]
    truncated: bool = False


class GraphTripletQuery(StrictModel):
    """Match canonical subject-predicate-object records by portable fields."""

    subject: str | None = None
    predicate: RelationType | None = None
    object: str | None = None
    limit: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def require_selector(self) -> Self:
        if not any((self.subject, self.predicate, self.object)):
            raise ValueError("triplet search requires a subject, predicate, or object")
        return self


class GraphTriplet(StrictModel):
    subject: GraphNodeRecord
    predicate: GraphEdgeRecord
    object: GraphNodeRecord

    @model_validator(mode="after")
    def validate_endpoints(self) -> Self:
        if self.predicate.source_node_key != self.subject.node_key:
            raise ValueError("triplet subject does not match the relation source")
        if self.predicate.target_node_key != self.object.node_key:
            raise ValueError("triplet object does not match the relation target")
        return self


class GraphTripletResult(StrictModel):
    triplets: tuple[GraphTriplet, ...]
    truncated: bool = False


class GraphPathQuery(StrictModel):
    """Find bounded paths between two portable node selectors."""

    start_node: str = Field(min_length=1)
    end_node: str = Field(min_length=1)
    relationship_types: tuple[RelationType, ...] = ()
    max_depth: int = Field(default=4, ge=1, le=8)
    max_paths: int = Field(default=10, ge=1, le=100)
    # BOTH, not OUTGOING: the spine is not uniformly directed. A chunk attaches to its
    # structure as (:Chunk)-[:SUPPORTS]->(:Structure), which points *into* the spine,
    # while (:DocumentVersion)-[:CONTAINS]->(:Structure) points down it. So the most
    # natural question -- which document does this chunk belong to -- traverses one edge
    # forwards and one backwards, and returns nothing when restricted to a single
    # direction. Callers wanting a directed path must now ask for it explicitly.
    direction: GraphDirection = GraphDirection.BOTH

    @model_validator(mode="after")
    def validate_endpoints(self) -> Self:
        if self.start_node == self.end_node:
            raise ValueError("graph path endpoints must be different")
        if len(set(self.relationship_types)) != len(self.relationship_types):
            raise ValueError("graph path relationship types must be unique")
        return self


class GraphPath(StrictModel):
    nodes: tuple[GraphNodeRecord, ...] = Field(min_length=2)
    relations: tuple[GraphEdgeRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if len(self.relations) != len(self.nodes) - 1:
            raise ValueError("graph path relations must connect each adjacent node")
        for left, relation, right in zip(
            self.nodes[:-1], self.relations, self.nodes[1:], strict=True
        ):
            endpoints = {relation.source_node_key, relation.target_node_key}
            if endpoints != {left.node_key, right.node_key}:
                raise ValueError("graph path relation does not connect its adjacent nodes")
        return self


class GraphPathResult(StrictModel):
    paths: tuple[GraphPath, ...]
    truncated: bool = False


class GraphSubgraphQuery(StrictModel):
    """Expand a bounded neighborhood around one portable node selector."""

    start_node: str = Field(min_length=1)
    relationship_types: tuple[RelationType, ...] = ()
    max_depth: int = Field(default=2, ge=1, le=8)
    max_nodes: int = Field(default=20, ge=1, le=100)
    direction: GraphDirection = GraphDirection.BOTH

    @model_validator(mode="after")
    def validate_relationship_types(self) -> Self:
        if len(set(self.relationship_types)) != len(self.relationship_types):
            raise ValueError("subgraph relationship types must be unique")
        return self


def compact_node(node: GraphNodeRecord) -> dict[str, object]:
    """Project a node to the fields a caller can act on.

    A full ``model_dump`` also carries ``owner_id``, ``graph_schema_version``,
    ``ownership_scope``, ``logical_id``, and the ``attributes`` blob. Those are write-side
    bookkeeping: they cannot be used as a selector for a follow-up query and cannot be
    cited, so for an LLM caller they are cost without benefit.
    """

    view: dict[str, object] = {
        "node_key": node.node_key,
        "node_kind": node.node_kind.value,
        "entity_type": node.entity_type.value,
    }
    if node.title is not None:
        view["title"] = node.title
    if node.section_path:
        view["section_path"] = list(node.section_path)
    if node.document_id is not None:
        view["document_id"] = str(node.document_id)
    if node.document_version_id is not None:
        view["document_version_id"] = str(node.document_version_id)
    if node.source_scope_id is not None:
        view["source_scope_id"] = node.source_scope_id
    return view


def compact_relation(relation: GraphEdgeRecord) -> dict[str, object]:
    """Project a relation to its predicate and endpoints."""

    view: dict[str, object] = {
        "relation_id": relation.relation_id,
        "relation_type": relation.relation_type.value,
        "source_node_key": relation.source_node_key,
        "target_node_key": relation.target_node_key,
        "origin": (
            "logical_view"
            if relation.attributes.get("logical_view") is True
            else "source_declared"
            if relation.source_explicit
            else "structural"
        ),
    }
    if relation.source_scope_id is not None:
        view["source_scope_id"] = relation.source_scope_id
    if relation.document_id is not None:
        view["document_id"] = str(relation.document_id)
    if relation.document_version_id is not None:
        view["document_version_id"] = str(relation.document_version_id)
    return view


def compact_triplet(triplet: GraphTriplet) -> dict[str, object]:
    return {
        "subject": compact_node(triplet.subject),
        "predicate": triplet.predicate.relation_type.value,
        "relation": compact_relation(triplet.predicate),
        "object": compact_node(triplet.object),
    }


def compact_path(path: GraphPath) -> dict[str, object]:
    return {
        "nodes": [compact_node(node) for node in path.nodes],
        "relations": [compact_relation(relation) for relation in path.relations],
    }
