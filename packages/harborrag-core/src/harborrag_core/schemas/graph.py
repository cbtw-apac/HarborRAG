from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from harborrag_core.base import StrictModel, utc_now
from harborrag_core.schemas.ids import EntityId, RelationshipId, TenantId


class GraphNode(StrictModel):
    """Provides graph node behavior."""

    id: EntityId
    tenant_id: TenantId
    labels: set[str] = Field(default_factory=set)
    properties: dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime = Field(default_factory=utc_now)
    valid_to: datetime | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    provenance: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(StrictModel):
    """Provides graph edge behavior."""

    id: RelationshipId
    tenant_id: TenantId
    source_id: EntityId
    target_id: EntityId
    relationship_type: str = Field(min_length=1)
    properties: dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime = Field(default_factory=utc_now)
    valid_to: datetime | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    provenance: dict[str, Any] = Field(default_factory=dict)


class GraphExpansionQuery(StrictModel):
    """Describes a validated graph expansion query."""

    start_nodes: list[EntityId] = Field(min_length=1)
    relationship_types: list[str] = Field(default_factory=list)
    max_depth: int = Field(default=2, ge=1, le=8)
    max_nodes: int = Field(default=200, ge=1, le=5000)
    direction: str = Field(default="both", pattern="^(incoming|outgoing|both)$")


class GraphSubgraph(StrictModel):
    """Represents graph subgraph data shared across HarborRAG layers."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool = False


class GraphStoreCapabilities(StrictModel):
    """Describes supported graph store features."""

    transactions: bool = True
    shortest_path: bool = False
    bounded_expansion: bool = True
    temporal_properties: bool = True
    constraints: bool = False
    indexes: bool = False
    advanced_native_query: bool = False
