"""Internal graph indexing plan and result schemas."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.schemas.graph import GraphEdge, GraphNode


@dataclass(frozen=True, slots=True)
class GraphMutationPlan:
    """Complete deterministic graph projection for one staged generation."""

    namespace: str
    generation_id: str
    artifact_id: str
    artifact_revision_id: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    chunk_node_ids: tuple[str, ...]
    retired_node_ids: tuple[str, ...]
    capsule_maximum_characters: int

    def __post_init__(self) -> None:
        """Validate plan identities, uniqueness, and capsule bounds."""

        if not all(
            value.strip()
            for value in (
                self.namespace,
                self.generation_id,
                self.artifact_id,
                self.artifact_revision_id,
            )
        ):
            raise ValueError("graph mutation plan identity values must be non-empty")
        if len({str(node.id) for node in self.nodes}) != len(self.nodes):
            raise ValueError("graph mutation plan node IDs must be unique")
        if len({str(edge.id) for edge in self.edges}) != len(self.edges):
            raise ValueError("graph mutation plan edge IDs must be unique")
        if self.capsule_maximum_characters < 1:
            raise ValueError("graph capsule limit must be positive")


@dataclass(frozen=True, slots=True)
class GraphValidationResult:
    """Read-after-write graph validation outcome."""

    valid: bool
    checked_node_count: int
    checked_edge_count: int
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphIndexResult:
    """Internal graph write result returned to combined orchestration."""

    plan: GraphMutationPlan
    validation: GraphValidationResult
