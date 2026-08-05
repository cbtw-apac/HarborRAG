"""Strict public retrieval request and response schemas."""

from __future__ import annotations

from typing import Self

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from harborrag_app.api.schemas import ApiModel
from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import GraphEdgeRecord, GraphNodeRecord
from harborrag_core.retrieval import GraphDirection, GraphPath, GraphTriplet
from harborrag_runtime.sdk import RetrievalLane


class TenantScopedRetrievalRequest(ApiModel):
    tenant: str = Field(
        default="DEFAULT",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )


class VectorSearchRequest(TenantScopedRetrievalRequest):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "query": "publication policy",
                    "tenant": "DEFAULT",
                    "top_k": 5,
                    "lane": "hybrid",
                    "include_content": True,
                    "include_metadata": True,
                }
            ]
        }
    )

    query: str = Field(min_length=1, max_length=16_384)
    top_k: int = Field(default=10, ge=1, le=100)
    lane: RetrievalLane = RetrievalLane.HYBRID
    filters: dict[str, JsonValue] | None = Field(
        default=None,
        description="Optional metadata equality filters; omit for an unfiltered search.",
    )
    observe_graph: bool = False
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    include_content: bool = True
    include_metadata: bool = True

    @field_validator("filters")
    @classmethod
    def reject_access_filters(
        cls,
        filters: dict[str, JsonValue] | None,
    ) -> dict[str, JsonValue] | None:
        if filters is not None and "tenant_id" in filters:
            raise ValueError("tenant must be provided as the top-level request field")
        return filters


class VectorSearchResultResponse(ApiModel):
    rank: int = Field(ge=1)
    id: str
    score: float
    source: str
    content: str | None = None
    metadata: dict[str, JsonValue] | None = None


class VectorSearchResponse(ApiModel):
    request_id: str
    lane: RetrievalLane
    results: list[VectorSearchResultResponse]
    diagnostics: dict[str, JsonValue]


class GraphTripletSearchRequest(TenantScopedRetrievalRequest):
    subject: str | None = Field(default=None, min_length=1)
    predicate: RelationType | None = None
    object: str | None = Field(default=None, min_length=1)
    limit: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def require_selector(self) -> Self:
        if not any((self.subject, self.predicate, self.object)):
            raise ValueError("triplet search requires a subject, predicate, or object")
        return self


class GraphPathSearchRequest(TenantScopedRetrievalRequest):
    start_node: str = Field(min_length=1)
    end_node: str = Field(min_length=1)
    relationship_types: list[RelationType] = Field(default_factory=list)
    max_depth: int = Field(default=4, ge=1, le=8)
    max_paths: int = Field(default=10, ge=1, le=100)
    # Matches GraphPathQuery: the spine mixes edge directions, so a directed default
    # returns nothing for the most common question.
    direction: GraphDirection = GraphDirection.BOTH

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        if self.start_node == self.end_node:
            raise ValueError("graph path endpoints must be different")
        if len(set(self.relationship_types)) != len(self.relationship_types):
            raise ValueError("graph path relationship types must be unique")
        return self


class GraphSubgraphSearchRequest(TenantScopedRetrievalRequest):
    start_node: str = Field(min_length=1)
    relationship_types: list[RelationType] = Field(default_factory=list)
    max_depth: int = Field(default=2, ge=1, le=8)
    max_nodes: int = Field(default=20, ge=1, le=100)
    direction: GraphDirection = GraphDirection.BOTH

    @model_validator(mode="after")
    def validate_relationship_types(self) -> Self:
        if len(set(self.relationship_types)) != len(self.relationship_types):
            raise ValueError("subgraph relationship types must be unique")
        return self


class GraphNeighborhoodSearchRequest(TenantScopedRetrievalRequest):
    """Expand the graph around a natural-language question, with no node selector."""

    query: str = Field(min_length=1)
    seed_limit: int = Field(default=3, ge=1, le=10)
    relationship_types: list[RelationType] = Field(default_factory=list)
    max_depth: int = Field(default=2, ge=1, le=8)
    max_nodes: int = Field(default=20, ge=1, le=100)
    direction: GraphDirection = GraphDirection.BOTH

    @model_validator(mode="after")
    def validate_relationship_types(self) -> Self:
        if len(set(self.relationship_types)) != len(self.relationship_types):
            raise ValueError("neighborhood relationship types must be unique")
        return self


class GraphSearchDiagnosticsResponse(ApiModel):
    candidate_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    stale_count: int = Field(ge=0)
    unpublished_count: int = Field(ge=0)
    projection_truncated: bool


class GraphTripletSearchResponse(ApiModel):
    triplets: list[GraphTriplet]
    diagnostics: GraphSearchDiagnosticsResponse


class GraphPathSearchResponse(ApiModel):
    paths: list[GraphPath]
    diagnostics: GraphSearchDiagnosticsResponse


class GraphSubgraphSearchResponse(ApiModel):
    nodes: list[GraphNodeRecord]
    relations: list[GraphEdgeRecord]
    diagnostics: GraphSearchDiagnosticsResponse


class GraphNeighborhoodSearchResponse(ApiModel):
    seeds: list[str]
    nodes: list[GraphNodeRecord]
    relations: list[GraphEdgeRecord]
    diagnostics: GraphSearchDiagnosticsResponse
