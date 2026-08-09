from __future__ import annotations

from harborrag_core.contracts import HarborConflictError, HarborValidationError
from harborrag_core.ingestion import (
    ArtifactReference,
    ChangeFingerprints,
    DocumentIdentityBuilder,
    DocumentVersionCandidate,
    DocumentVersionSnapshot,
    DocumentVersionState,
    identity_for_source,
)
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from .row_values import DatabaseRow, required_text


def snapshot_from_row(row: DatabaseRow) -> DocumentVersionSnapshot:
    def artifact(column: str) -> ArtifactReference | None:
        value = row[column]
        return ArtifactReference.model_validate(value) if value is not None else None

    return DocumentVersionSnapshot(
        document_id=DocumentId(required_text(row, "document_id")),
        document_version_id=DocumentVersionId(required_text(row, "document_version_id")),
        fingerprints=ChangeFingerprints(
            admission_change_key=required_text(
                row,
                "admission_change_key",
            ),
            canonical_content_hash=required_text(
                row,
                "canonical_content_hash",
            ),
            retrieval_metadata_hash=required_text(
                row,
                "retrieval_metadata_hash",
            ),
            processing_fingerprint=required_text(
                row,
                "processing_fingerprint",
            ),
        ),
        state=DocumentVersionState(required_text(row, "status")),
        raw_artifact=artifact("raw_artifact"),
        raw_metadata_artifact=artifact("raw_metadata_artifact"),
        canonical_artifact=artifact("canonical_artifact"),
        chunk_artifact=artifact("chunk_artifact"),
        chunk_index_artifact=artifact("chunk_index_artifact"),
        relation_artifact=artifact("relation_artifact"),
        representation_artifact=artifact("representation_artifact"),
    )


def replay_state_from_row(row: DatabaseRow) -> DocumentVersionState:
    boundaries = (
        ("representation_artifact", DocumentVersionState.REPRESENTATIONS_READY),
        ("chunk_artifact", DocumentVersionState.CHUNKS_READY),
        ("canonical_artifact", DocumentVersionState.CANONICAL_READY),
        ("raw_artifact", DocumentVersionState.RAW_CAPTURED),
    )
    return next(
        (state for column, state in boundaries if row[column] is not None),
        DocumentVersionState.PENDING,
    )


def validate_candidate_identity(candidate: DocumentVersionCandidate) -> None:
    if identity_for_source(candidate.source_identity) != candidate.document_id:
        raise HarborValidationError("candidate document identity is not deterministic")
    expected_version = DocumentIdentityBuilder().document_version_id(
        document_id=str(candidate.document_id),
        canonical_content_hash=candidate.fingerprints.canonical_content_hash,
        retrieval_metadata_hash=candidate.fingerprints.retrieval_metadata_hash,
        processing_fingerprint=candidate.fingerprints.processing_fingerprint,
    )
    if expected_version != candidate.document_version_id:
        raise HarborValidationError("candidate document-version identity is not deterministic")


def validate_document_source(
    row: DatabaseRow,
    candidate: DocumentVersionCandidate,
) -> None:
    expected = (
        candidate.source_identity.tenant_id,
        candidate.source_identity.connector_type.value,
        candidate.source_identity.connection_id,
        candidate.source_identity.source_item_id,
    )
    actual = (
        row["tenant_id"],
        row["connector_type"],
        row["connection_id"],
        row["source_item_id"],
    )
    if actual != expected:
        raise HarborConflictError("document identity is bound to another source")


def validate_immutable_version(
    row: DatabaseRow,
    candidate: DocumentVersionCandidate,
) -> None:
    actual = (
        row["document_id"],
        row["canonical_content_hash"],
        row["retrieval_metadata_hash"],
        row["processing_fingerprint"],
        row["admission_change_key"],
    )
    expected = (
        str(candidate.document_id),
        candidate.fingerprints.canonical_content_hash,
        candidate.fingerprints.retrieval_metadata_hash,
        candidate.fingerprints.processing_fingerprint,
        candidate.fingerprints.admission_change_key,
    )
    if actual != expected:
        raise HarborConflictError("document-version identity collision")
