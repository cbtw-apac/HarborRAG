from __future__ import annotations

from dataclasses import dataclass

from .schemas import ProcessingProfileInput


@dataclass(frozen=True, slots=True)
class ProjectionCleanupResult:
    claimed: int
    completed: int
    cancelled: int
    failed: int

    def __post_init__(self) -> None:
        counts = (
            self.claimed,
            self.completed,
            self.cancelled,
            self.failed,
        )
        if any(count < 0 for count in counts):
            raise ValueError("cleanup counts must not be negative")
        if self.completed + self.cancelled + self.failed != self.claimed:
            raise ValueError("cleanup outcomes must equal claimed jobs")


@dataclass(frozen=True, slots=True)
class RelationRepairResult:
    repaired_documents: int
    resolved_relations: int
    unresolved_relations: int

    def __post_init__(self) -> None:
        counts = (
            self.repaired_documents,
            self.resolved_relations,
            self.unresolved_relations,
        )
        if any(count < 0 for count in counts):
            raise ValueError("relation repair counts must not be negative")


@dataclass(frozen=True, slots=True)
class ReindexInput:
    reindex_job_id: str
    tenant_id: str
    processing: ProcessingProfileInput
    document_id: str | None = None
    limit: int = 10_000

    def __post_init__(self) -> None:
        if not self.reindex_job_id.strip() or not self.tenant_id.strip():
            raise ValueError("reindex identities must be non-empty")
        if not 1 <= self.limit <= 100_000:
            raise ValueError("reindex limit must be between 1 and 100000")


@dataclass(frozen=True, slots=True)
class ReindexResult:
    reindex_job_id: str
    status: str
    connector_call_count: int
    scanned_count: int
    processed_count: int
    published_count: int
    skipped_count: int
    failure_count: int
    last_error_code: str | None = None
