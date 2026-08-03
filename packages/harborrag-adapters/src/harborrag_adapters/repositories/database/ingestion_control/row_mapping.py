from __future__ import annotations

from harborrag_core.ingestion import CleanupJobState, ProjectionCleanupJob
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from .row_values import (
    DatabaseRow,
    optional_text,
    required_int,
    required_text,
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
