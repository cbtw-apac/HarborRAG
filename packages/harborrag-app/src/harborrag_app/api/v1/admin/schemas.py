"""Public schemas for tenant-scoped projection administration."""

from __future__ import annotations

from typing import Literal

from harborrag_app.api.schemas import ApiModel


class VectorCollectionInventory(ApiModel):
    logical_name: Literal["routes", "evidence"]
    physical_name: str
    exists: bool


class ProjectionInventoryResponse(ApiModel):
    tenant: str
    vector_collections: list[VectorCollectionInventory]
    graph_name: str
    graph_nodes: int
    graph_relations: int


class ProjectionDeletionResponse(ApiModel):
    tenant: str
    deleted_stores: list[Literal["graph", "vector"]]
    before: ProjectionInventoryResponse
    reindex_required: bool
