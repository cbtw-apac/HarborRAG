"""Schemas owned by the engine indexing stage."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from harborrag_core.schemas.documents import ChunkRecord
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_engine.ingestion.chunking.schemas import (
    ChunkingResult,
    ChunkManifest,
    ChunkReference,
)

from .config import IndexingConfig


class ChunkDiffStatus(StrEnum):
    """Classify one logical chunk relative to the active revision."""

    NEW = "NEW"
    UNCHANGED = "UNCHANGED"
    REFRESH_REQUIRED = "REFRESH_REQUIRED"
    CHANGED = "CHANGED"
    REMOVED = "REMOVED"
    REEMBED_REQUIRED = "REEMBED_REQUIRED"


EMBEDDING_STATUSES = frozenset(
    {
        ChunkDiffStatus.NEW,
        ChunkDiffStatus.CHANGED,
        ChunkDiffStatus.REEMBED_REQUIRED,
    }
)


@dataclass(frozen=True, slots=True)
class ChunkDiffEntry:
    """One deterministic comparison result keyed by logical chunk identity."""

    logical_chunk_id: str
    status: ChunkDiffStatus
    previous: ChunkReference | None
    current: ChunkReference | None

    def __post_init__(self) -> None:
        """Validate that identities and status-specific references agree."""

        if not self.logical_chunk_id.strip():
            raise ValueError("logical_chunk_id must be non-empty")
        if self.previous is None and self.current is None:
            raise ValueError("a diff entry requires a previous or current reference")
        for reference in (self.previous, self.current):
            if reference is not None and reference.logical_chunk_id != self.logical_chunk_id:
                raise ValueError("diff reference does not match logical_chunk_id")
        if self.status is ChunkDiffStatus.NEW and (
            self.previous is not None or self.current is None
        ):
            raise ValueError("NEW requires only a current reference")
        if self.status is ChunkDiffStatus.REMOVED and (
            self.previous is None or self.current is not None
        ):
            raise ValueError("REMOVED requires only a previous reference")
        if self.status not in {ChunkDiffStatus.NEW, ChunkDiffStatus.REMOVED} and (
            self.previous is None or self.current is None
        ):
            raise ValueError(f"{self.status.value} requires both references")

    @property
    def requires_embedding(self) -> bool:
        """Return whether this entry needs a new embedding."""

        return self.status in EMBEDDING_STATUSES


@dataclass(frozen=True, slots=True)
class ChunkDiffResult:
    """Complete ordered diff between an active and proposed chunk manifest."""

    entries: tuple[ChunkDiffEntry, ...]
    active_manifest_fingerprint: str | None
    proposed_manifest_fingerprint: str
    active_embedding_configuration_fingerprint: str | None
    target_embedding_configuration_fingerprint: str

    @property
    def for_embedding(self) -> tuple[ChunkDiffEntry, ...]:
        """Return entries that require embedding in manifest order."""

        return tuple(entry for entry in self.entries if entry.requires_embedding)

    @property
    def for_refresh(self) -> tuple[ChunkDiffEntry, ...]:
        """Return content-stable entries whose indexed metadata changed."""

        return tuple(
            entry for entry in self.entries if entry.status is ChunkDiffStatus.REFRESH_REQUIRED
        )

    @property
    def removed(self) -> tuple[ChunkDiffEntry, ...]:
        """Return entries removed from the proposed manifest."""

        return tuple(entry for entry in self.entries if entry.status is ChunkDiffStatus.REMOVED)

    def count(self, status: ChunkDiffStatus) -> int:
        """Count entries with the requested diff status."""

        return sum(entry.status is status for entry in self.entries)


@dataclass(frozen=True, slots=True)
class PreparedEmbeddingInput:
    """Canonical chunk plus separately rendered, bounded embedding text."""

    record: ChunkRecord
    text: str
    token_count: int

    def __post_init__(self) -> None:
        """Validate prepared text and its positive token count."""

        if not self.text.strip() or self.token_count < 1:
            raise ValueError("prepared embedding input text/tokens are invalid")


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """One deterministic model-ready group of prepared chunk inputs."""

    ordinal: int
    inputs: tuple[PreparedEmbeddingInput, ...]
    total_tokens: int

    def __post_init__(self) -> None:
        """Validate batch ordering, contents, and aggregate token count."""

        if self.ordinal < 0 or not self.inputs:
            raise ValueError("embedding batch ordinal/inputs are invalid")
        if self.total_tokens != sum(item.token_count for item in self.inputs):
            raise ValueError("embedding batch token total does not match inputs")


@dataclass(frozen=True, slots=True)
class EmbeddedChunk:
    """A canonical chunk paired with its normalized dense embedding."""

    record: ChunkRecord
    vector: tuple[float, ...]

    def __post_init__(self) -> None:
        """Ensure the embedded chunk carries a non-empty vector."""

        if not self.vector:
            raise ValueError("embedded chunk vector must not be empty")


@dataclass(frozen=True, slots=True)
class EmbeddingRun:
    """Normalized output of all engine-planned embedding batches."""

    chunks: tuple[EmbeddedChunk, ...]
    configuration_fingerprint: str
    dimension: int | None
    embedding_space: str | None

    def __post_init__(self) -> None:
        """Validate the run fingerprint and consistent vector dimensions."""

        if not self.configuration_fingerprint.strip():
            raise ValueError("embedding configuration fingerprint must be non-empty")
        if self.chunks:
            dimensions = {len(chunk.vector) for chunk in self.chunks}
            if len(dimensions) != 1 or self.dimension != next(iter(dimensions)):
                raise ValueError("embedding run dimensions are inconsistent")
            if not self.embedding_space:
                raise ValueError("non-empty embedding run requires an embedding space")
        elif self.dimension is not None or self.embedding_space is not None:
            raise ValueError("empty embedding run cannot declare vector metadata")


class IndexingStatus(StrEnum):
    """Describe the cross-store outcome of one indexing generation."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class IndexingFailure:
    """Sanitized provider failure safe to persist in workflow state."""

    component: str
    error_type: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class IndexingDiagnostics:
    """Deterministic counts for one combined indexing execution."""

    new_chunks: int
    unchanged_chunks: int
    changed_chunks: int
    removed_chunks: int
    reembedded_chunks: int
    embedded_chunks: int
    embedding_batches: int
    vector_upserts: int
    vector_retentions: int
    vector_retirements: int
    vector_deletions: int
    vector_tombstones: int
    graph_nodes: int
    graph_edges: int


@dataclass(frozen=True, slots=True)
class IndexingRequest:
    """Validated chunking output plus explicit indexing generation context."""

    chunking: ChunkingResult
    generation_id: str
    config: IndexingConfig
    context: StorageOperationContext
    active_manifest: ChunkManifest | None = None
    active_embedding_configuration_fingerprint: str | None = None
    active_generation_id: str | None = None
    resume_result: IndexingResult | None = None

    def __post_init__(self) -> None:
        """Validate generation context and canonical chunking input."""

        if not self.generation_id.strip():
            raise ValueError("generation_id must be non-empty")
        if not self.chunking.manifest.validation.valid:
            raise ValueError("indexing requires a validated chunk manifest")
        if str(self.context.tenant_id) != self.chunking.manifest.tenant_id:
            raise ValueError("storage context tenant does not match chunk manifest")
        if self.active_manifest is not None and (
            self.active_manifest.tenant_id != self.chunking.manifest.tenant_id
            or self.active_manifest.artifact_id != self.chunking.manifest.artifact_id
        ):
            raise ValueError("active and proposed manifests must describe one artifact")
        if self.active_manifest is None and (
            self.active_embedding_configuration_fingerprint is not None
            or self.active_generation_id is not None
        ):
            raise ValueError("active indexing metadata requires an active manifest")
        if self.resume_result is not None and (
            self.resume_result.artifact_id != self.chunking.manifest.artifact_id
            or self.resume_result.artifact_revision_id
            != self.chunking.manifest.artifact_revision_id
            or self.resume_result.generation_id != self.generation_id
        ):
            raise ValueError("indexing resume checkpoint does not match the request")


@dataclass(frozen=True, slots=True)
class IndexingResult:
    """Provider-independent combined indexing outcome without raw vectors."""

    artifact_id: str
    artifact_revision_id: str
    generation_id: str
    status: IndexingStatus
    vector_valid: bool
    graph_valid: bool
    validation_errors: tuple[str, ...]
    diagnostics: IndexingDiagnostics
    activation: GenerationActivationPlan
    failures: tuple[IndexingFailure, ...] = ()


@dataclass(frozen=True, slots=True)
class GenerationActivationPlan:
    """Provider mutations deferred until staged indexing has been validated."""

    artifact_id: str
    generation_id: str
    previous_generation_id: str | None
    vector_collection: str
    activate_vector_ids: tuple[str, ...]
    retire_vector_ids: tuple[str, ...]
    delete_vector_ids: tuple[str, ...]
    tombstone_vector_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject incomplete or contradictory mutation identities."""

        if not self.artifact_id.strip():
            raise ValueError("artifact_id must be non-empty")
        if not self.generation_id.strip():
            raise ValueError("generation_id must be non-empty")
        if not self.vector_collection.strip():
            raise ValueError("vector_collection must be non-empty")
        if self.previous_generation_id == self.generation_id:
            raise ValueError("previous and proposed generations must differ")
        groups = (
            self.activate_vector_ids,
            self.retire_vector_ids,
            self.delete_vector_ids,
            self.tombstone_vector_ids,
        )
        if any(not identity.strip() for group in groups for identity in group):
            raise ValueError("vector activation identities must be non-empty")
        all_ids = [identity for group in groups for identity in group]
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("a vector identity cannot have multiple activation actions")


@dataclass(frozen=True, slots=True)
class GenerationActivationRequest:
    """Bind a durable activation plan to its tenant-scoped storage context."""

    plan: GenerationActivationPlan
    context: StorageOperationContext
