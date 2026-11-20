from __future__ import annotations

from harborrag_core.ingestion import CleanupJobState, DocumentFailureRecord, ProjectionCleanupJob
from harborrag_core.ingestion.states import FailureCategory
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from .row_values import (
    DatabaseRow,
    optional_text,
    required_bool,
    required_datetime,
    required_int,
    required_text,
)


def document_failure_record_from_row(
    row: DatabaseRow,
) -> DocumentFailureRecord:
    """Map one `document_failures` row without leaking SQLAlchemy types."""

    return DocumentFailureRecord(
        document_id=DocumentId(required_text(row, "document_id")),
        document_version_id=DocumentVersionId(required_text(row, "document_version_id")),
        failed_stage=required_text(row, "failed_stage"),
        category=FailureCategory(required_text(row, "failure_category")),
        retryable=required_bool(row, "retryable"),
        safe_error_code=required_text(row, "safe_error_code"),
        attempt_count=required_int(row, "attempt_count"),
        first_failure_at=required_datetime(row, "first_failure_at"),
        last_failure_at=required_datetime(row, "last_failure_at"),
    )


def cleanup_job_from_row(
    row: DatabaseRow,
) -> ProjectionCleanupJob:
    """Map one durable cleanup row without leaking SQLAlchemy types."""

    return ProjectionCleanupJob(
        cleanup_job_id=required_text(row, "cleanup_job_id"),
        document_id=DocumentId(required_text(row, "document_id")),
        document_version_id=DocumentVersionId(required_text(row, "document_version_id")),
        status=CleanupJobState(required_text(row, "status")),
        attempt_count=required_int(row, "attempt_count"),
        last_error_code=optional_text(row, "last_error_code"),
    )
