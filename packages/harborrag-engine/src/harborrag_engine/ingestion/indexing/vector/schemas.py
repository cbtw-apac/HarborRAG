"""Internal vector indexing plan and result schemas."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from harborrag_core.schemas.vector import VectorPoint

from ..schemas import ChunkDiffResult, EmbeddingBatch


class VectorMutationAction(StrEnum):
    """Describe idempotent vector writes and deferred lifecycle intent."""

    UPSERT = "UPSERT"
    RETAIN = "RETAIN"
    RETIRE = "RETIRE"
    DELETE = "DELETE"
    TOMBSTONE = "TOMBSTONE"


@dataclass(frozen=True, slots=True)
class VectorMutation:
    """One logical vector action derived from a chunk diff entry."""

    action: VectorMutationAction
    logical_chunk_id: str
    current_chunk_revision_id: str | None = None
    previous_chunk_revision_id: str | None = None
    point_id: str | None = None
    point: VectorPoint | None = None

    def __post_init__(self) -> None:
        """Validate action-specific vector mutation fields."""

        if not self.logical_chunk_id.strip():
            raise ValueError("vector mutation logical identity must be non-empty")
        if self.action is VectorMutationAction.UPSERT and (
            self.point is None or self.point_id != self.point.id
        ):
            raise ValueError("UPSERT vector mutation requires its deterministic point")
        if self.action is not VectorMutationAction.UPSERT and self.point is not None:
            raise ValueError("only UPSERT vector mutations carry vector points")


@dataclass(frozen=True, slots=True)
class VectorMutationPlan:
    """Generation-scoped vector actions with activation changes deferred."""

    collection: str
    generation_id: str
    embedding_configuration_fingerprint: str
    mutations: tuple[VectorMutation, ...]
    dimension: int

    def __post_init__(self) -> None:
        """Validate plan identities, dimensions, and point uniqueness."""

        if not all(
            value.strip()
            for value in (
                self.collection,
                self.generation_id,
                self.embedding_configuration_fingerprint,
            )
        ):
            raise ValueError("vector mutation plan identity values must be non-empty")
        if self.dimension < 1:
            raise ValueError("vector mutation plan dimension must be positive")
        point_ids = [point.id for point in self.points]
        if len(set(point_ids)) != len(point_ids):
            raise ValueError("vector mutation plan point IDs must be unique")

    @property
    def points(self) -> tuple[VectorPoint, ...]:
        """Return vector points carried by upsert mutations."""

        return tuple(mutation.point for mutation in self.mutations if mutation.point is not None)

    def count(self, action: VectorMutationAction) -> int:
        """Count mutations with the requested lifecycle action."""

        return sum(mutation.action is action for mutation in self.mutations)


@dataclass(frozen=True, slots=True)
class VectorValidationResult:
    """Read-after-write validation for staged vector upserts."""

    valid: bool
    checked_point_count: int
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VectorIndexResult:
    """Internal vector execution result; raw vectors stay below the public result."""

    diff: ChunkDiffResult
    batches: tuple[EmbeddingBatch, ...]
    plan: VectorMutationPlan
    validation: VectorValidationResult
