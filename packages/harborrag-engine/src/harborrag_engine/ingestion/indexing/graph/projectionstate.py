"""Deterministic identities and mutable state for one graph projection."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from harborrag_core.schemas.graph import GraphEdge, GraphNode
from harborrag_core.schemas.ids import EntityId, RelationshipId, TenantId


def deterministic_graph_node_id(
    *,
    namespace: str,
    tenant_id: str,
    generation_id: str,
    artifact_id: str,
    kind: str,
    key: str,
) -> str:
    """Return a deterministic artifact- and generation-scoped graph node identity."""

    values = (
        "harborrag-graph-node-v2",
        namespace,
        tenant_id,
        generation_id,
        artifact_id,
        kind,
        key,
    )
    if not all(value.strip() for value in values):
        raise ValueError("graph node identity values must be non-empty")
    return f"node:{sha256(chr(31).join(values).encode()).hexdigest()}"


def deterministic_graph_edge_id(
    *,
    namespace: str,
    tenant_id: str,
    generation_id: str,
    relationship_type: str,
    source_id: str,
    target_id: str,
    qualifier: str = "",
) -> str:
    """Return a deterministic generation-scoped graph edge identity."""

    values = (
        "harborrag-graph-edge-v1",
        namespace,
        tenant_id,
        generation_id,
        relationship_type,
        source_id,
        target_id,
        qualifier,
    )
    if any(not value.strip() for value in values[:-1]):
        raise ValueError("graph edge identity values must be non-empty")
    return f"edge:{sha256(chr(31).join(values).encode()).hexdigest()}"


class GraphProjectionState:
    """Accumulate one generation while rejecting conflicting deterministic IDs."""

    def __init__(
        self,
        *,
        namespace: str,
        tenant_id: str,
        generation_id: str,
        artifact_id: str,
        artifact_revision_id: str,
    ) -> None:
        """Initialize identity context and empty graph record registries."""

        self.namespace = namespace
        self.tenant_id = tenant_id
        self.generation_id = generation_id
        self.artifact_id = artifact_id
        self.artifact_revision_id = artifact_revision_id
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}

    def node(
        self,
        *,
        kind: str,
        key: str,
        labels: set[str],
        properties: dict[str, Any] | None = None,
    ) -> str:
        """Register or reuse a deterministic graph node."""

        node_id = deterministic_graph_node_id(
            namespace=self.namespace,
            tenant_id=self.tenant_id,
            generation_id=self.generation_id,
            artifact_id=self.artifact_id,
            kind=kind,
            key=key,
        )
        node = GraphNode(
            id=EntityId(node_id),
            tenant_id=TenantId(self.tenant_id),
            labels=labels,
            properties={**self._common_properties(), **(properties or {})},
            provenance={"projection": "deterministic"},
        )
        existing = self.nodes.get(node_id)
        if existing is not None and (
            existing.labels != node.labels or existing.properties != node.properties
        ):
            raise ValueError(f"conflicting graph node projection for {node_id}")
        self.nodes[node_id] = node
        return node_id

    def edge(
        self,
        relationship_type: str,
        source_id: str,
        target_id: str,
        *,
        qualifier: str = "",
        properties: dict[str, Any] | None = None,
    ) -> str:
        """Register or reuse a deterministic graph edge."""

        edge_id = deterministic_graph_edge_id(
            namespace=self.namespace,
            tenant_id=self.tenant_id,
            generation_id=self.generation_id,
            relationship_type=relationship_type,
            source_id=source_id,
            target_id=target_id,
            qualifier=qualifier,
        )
        edge = GraphEdge(
            id=RelationshipId(edge_id),
            tenant_id=TenantId(self.tenant_id),
            source_id=EntityId(source_id),
            target_id=EntityId(target_id),
            relationship_type=relationship_type,
            properties={**self._common_properties(), **(properties or {})},
            provenance={"projection": "deterministic"},
        )
        existing = self.edges.get(edge_id)
        if existing is not None and (
            existing.source_id != edge.source_id
            or existing.target_id != edge.target_id
            or existing.relationship_type != edge.relationship_type
            or existing.properties != edge.properties
        ):
            raise ValueError(f"conflicting graph edge projection for {edge_id}")
        self.edges[edge_id] = edge
        return edge_id

    def _common_properties(self) -> dict[str, str | bool]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_revision_id": self.artifact_revision_id,
            "generation_id": self.generation_id,
            "index_state": "staged",
            "is_active": False,
        }
