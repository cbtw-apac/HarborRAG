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


class GraphNeighborhoodQuery(StrictModel):
    """Expand the graph around whatever a natural-language query retrieves.

    Every other graph query needs a node selector the caller must already possess:
    a ``node_key`` is an opaque hash, ``logical_id`` is internal, and ``title`` is unset
    on chunk nodes and otherwise matches only in full. This query removes that
    precondition by resolving its own seeds through the vector index, which is the one
    component that accepts free text.
    """

    query: str = Field(min_length=1)
    seed_limit: int = Field(default=3, ge=1, le=10)
    relationship_types: tuple[RelationType, ...] = ()
    max_depth: int = Field(default=2, ge=1, le=8)
    max_nodes: int = Field(default=20, ge=1, le=100)
    direction: GraphDirection = GraphDirection.BOTH

    @model_validator(mode="after")
    def validate_relationship_types(self) -> Self:
        if len(set(self.relationship_types)) != len(self.relationship_types):
            raise ValueError("neighborhood relationship types must be unique")
        if not self.query.strip():
            raise ValueError("neighborhood query must not be blank")
        return self

    def to_subgraph_query(self, start_node: str) -> GraphSubgraphQuery:
        """Build the per-seed expansion that this neighborhood fans out into."""

        return GraphSubgraphQuery(
            start_node=start_node,
            relationship_types=self.relationship_types,
            max_depth=self.max_depth,
            max_nodes=self.max_nodes,
            direction=self.direction,
        )


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
    return view


def compact_relation(relation: GraphEdgeRecord) -> dict[str, object]:
    """Project a relation to its predicate and endpoints."""

    return {
        "relation_type": relation.relation_type.value,
        "source_node_key": relation.source_node_key,
        "target_node_key": relation.target_node_key,
    }


def compact_triplet(triplet: GraphTriplet) -> dict[str, object]:
    return {
        "subject": compact_node(triplet.subject),
        "predicate": triplet.predicate.relation_type.value,
        "object": compact_node(triplet.object),
    }


def compact_path(path: GraphPath) -> dict[str, object]:
    return {
        "nodes": [compact_node(node) for node in path.nodes],
        "relations": [compact_relation(relation) for relation in path.relations],
    }
