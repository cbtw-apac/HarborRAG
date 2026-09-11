"""Strict public schemas for graph screen endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from harborrag_app.api.schemas import ApiModel
from harborrag_core.chunking import PROJECTED_RELATION_TYPES, RelationType
from harborrag_core.domain.graph_conflict import GraphConflict
from harborrag_core.ingestion import GraphEdgeRecord, GraphNodeRecord
from harborrag_core.retrieval import GraphDirection

from ..retrieval.schemas import GraphSearchDiagnosticsResponse, TenantScopedRetrievalRequest

_MAX_GRAPH_NODE_LENGTH = 1_024

ConflictActionLiteral = Literal["replace", "version", "append", "skip", "archive", "merge"]


def _reject_unprojected_relation_types(values: Any) -> None:
    if not isinstance(values, list | tuple):
        values = (values,)
    unprojected = sorted({str(item) for item in values if item not in PROJECTED_RELATION_TYPES})
    if unprojected:
        allowed = ", ".join(item.value for item in PROJECTED_RELATION_TYPES)
        raise ValueError(f"unsupported relation type(s) {unprojected}; must be one of {allowed}")


class GraphOverviewResponse(ApiModel):
    tenant: str
    graph_name: str
    node_count: int = Field(ge=0)
    relation_count: int = Field(ge=0)


class GraphTraverseRequest(TenantScopedRetrievalRequest):
    """Bounded neighborhood expansion from one seed node.

    Mirrors ``GraphSubgraphQuery`` (the only bounded-traversal capability the retrieval
    stack currently exposes): a single ``start_node``, not the arch plan's multi-seed
    ``seed_nodes`` shape, which has no backing repository capability yet.
    """

    start_node: str = Field(min_length=1, max_length=_MAX_GRAPH_NODE_LENGTH)
    relationship_types: list[RelationType] = Field(default_factory=list)
    max_depth: int = Field(default=2, ge=1, le=8)
    max_nodes: int = Field(default=20, ge=1, le=100)
    direction: GraphDirection = GraphDirection.BOTH

    @field_validator("relationship_types", mode="before")
    @classmethod
    def validate_projected_relationship_types(cls, value: Any) -> Any:
        _reject_unprojected_relation_types(value)
        return value

    @model_validator(mode="after")
    def validate_relationship_types(self) -> Self:
        if len(set(self.relationship_types)) != len(self.relationship_types):
            raise ValueError("traverse relationship types must be unique")
        return self


class GraphTraverseResponse(ApiModel):
    nodes: list[GraphNodeRecord]
    relations: list[GraphEdgeRecord]
    diagnostics: GraphSearchDiagnosticsResponse


class GraphConflictResponse(ApiModel):
    """One queued graph disagreement; v1 is record-only (see GraphConflict's docstring)."""

    id: str
    tenant_id: str
    conflict_type: str
    subject_node_key: str
    competing_node_key: str | None
    description: str
    status: Literal["open", "resolved"]
    action: ConflictActionLiteral | None
    resolved_by: str | None
    detected_at: datetime
    resolved_at: datetime | None

    @classmethod
    def from_domain(cls, conflict: GraphConflict) -> GraphConflictResponse:
        return cls(
            id=conflict.id,
            tenant_id=conflict.tenant_id,
            conflict_type=conflict.conflict_type,
            subject_node_key=conflict.subject_node_key,
            competing_node_key=conflict.competing_node_key,
            description=conflict.description,
            status=conflict.status,
            action=conflict.action,
            resolved_by=conflict.resolved_by,
            detected_at=conflict.detected_at,
            resolved_at=conflict.resolved_at,
        )


class GraphConflictListResponse(ApiModel):
    conflicts: list[GraphConflictResponse]
    next_cursor: str | None


class GraphConflictResolveRequest(ApiModel):
    action: ConflictActionLiteral
