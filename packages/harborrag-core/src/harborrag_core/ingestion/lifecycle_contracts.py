from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from .artifact_contracts import ArtifactReference
from .source_contracts import ChangeFingerprints
from .states import (
    CleanupJobState,
    DocumentVersionState,
    FailureCategory,
    IngestionTaskState,
    ReindexJobState,
)


class DocumentFailure(StrictModel):
    """Safe durable failure information; stack data belongs in telemetry."""

    document_id: DocumentId
    document_version_id: DocumentVersionId
    failed_stage: str = Field(min_length=1)
    category: FailureCategory
    retryable: bool
    safe_error_code: str = Field(min_length=1)
    artifact_references: tuple[ArtifactReference, ...] = ()
    projection_manifest_reference: ArtifactReference | None = None


class ProjectionCleanupJob(StrictModel):
    cleanup_job_id: str = Field(min_length=1)
    document_id: DocumentId
    document_version_id: DocumentVersionId
    status: CleanupJobState
    attempt_count: int = Field(ge=0)
    last_error_code: str | None = None


class ReindexProgress(StrictModel):
    """Aggregate result counts for one replay-safe reindex pass."""

    scanned_count: int = Field(default=0, ge=0)
    processed_count: int = Field(default=0, ge=0)
    published_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> ReindexProgress:
        classified = self.published_count + self.skipped_count + self.failure_count
        if classified > self.processed_count:
            raise ValueError("reindex result counts exceed processed documents")
        if self.processed_count > self.scanned_count:
            raise ValueError("reindex processed count exceeds scanned documents")
        return self


class ReindexJob(StrictModel):
    """Durable progress for a connector-free canonical reprojection."""

    reindex_job_id: str = Field(min_length=1)
    document_id: DocumentId | None = None
    status: ReindexJobState
    target_processing_fingerprint: str = Field(min_length=1)
    connector_call_count: int = Field(default=0, ge=0)
    scanned_count: int = Field(default=0, ge=0)
    processed_count: int = Field(default=0, ge=0)
    published_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    last_error_code: str | None = None

    @model_validator(mode="after")
    def validate_progress(self) -> ReindexJob:
        if self.connector_call_count != 0:
            raise ValueError("reindex jobs must not call source connectors")
        ReindexProgress(
            scanned_count=self.scanned_count,
            processed_count=self.processed_count,
            published_count=self.published_count,
            skipped_count=self.skipped_count,
            failure_count=self.failure_count,
        )
        return self


class IngestionTask(StrictModel):
    task_id: str = Field(min_length=1)
    source_scope_id: str = Field(min_length=1)
    status: IngestionTaskState
    request: dict[str, object]
    summary: dict[str, object] = Field(default_factory=dict)
    submitted_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class TaskDocumentResult(StrictModel):
    task_id: str = Field(min_length=1)
    document_id: DocumentId
    document_version_id: DocumentVersionId | None = None
    status: str = Field(min_length=1)
    result: dict[str, object] = Field(default_factory=dict)


class ActiveDocumentVersion(StrictModel):
    document_id: DocumentId
    document_version_id: DocumentVersionId


class DocumentVersionSnapshot(StrictModel):
    """Durable document-version state needed for replay and admission."""

    document_id: DocumentId
    document_version_id: DocumentVersionId
    fingerprints: ChangeFingerprints
    state: DocumentVersionState
    raw_artifact: ArtifactReference | None = None
    raw_metadata_artifact: ArtifactReference | None = None
    canonical_artifact: ArtifactReference | None = None
    chunk_artifact: ArtifactReference | None = None
    chunk_index_artifact: ArtifactReference | None = None
    relation_artifact: ArtifactReference | None = None
    representation_artifact: ArtifactReference | None = None


class PublicationResult(StrictModel):
    document_id: DocumentId
    active_document_version_id: DocumentVersionId
    retired_document_version_id: DocumentVersionId | None = None
    cleanup_job_created: bool = False
    replayed: bool = False


class DocumentRetirementResult(StrictModel):
    document_id: DocumentId
    retired_document_version_id: DocumentVersionId | None = None
    cleanup_job_created: bool = False
    replayed: bool = False
