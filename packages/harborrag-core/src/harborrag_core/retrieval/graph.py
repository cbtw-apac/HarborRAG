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
    direction: GraphDirection = GraphDirection.OUTGOING

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
