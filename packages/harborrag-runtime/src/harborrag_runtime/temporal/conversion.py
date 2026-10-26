from __future__ import annotations

from harborrag_core.ingestion import (
    ArtifactReference,
    RawDocumentReference,
    SourceAdmissionDecision,
)
from harborrag_core.invariants import HarborInvariantError
from harborrag_core.schemas.ids import DocumentId
from harborrag_runtime.execution.source_conversion import (
    to_processing_profile as to_processing_profile,
)
from harborrag_runtime.execution.source_conversion import (
    to_source_request as to_source_request,
)
from harborrag_runtime.ingestion.document.stage_models import (
    PreparedDocumentStage,
    RawCaptureStageResult,
)

from .schemas import (
    DocumentIngestionInput,
    PreparedDocument,
    RawCaptureResult,
    WorkflowArtifactReference,
)


def to_raw_capture_result(
    document: DocumentIngestionInput,
    capture: RawCaptureStageResult,
) -> RawCaptureResult:
    raw = capture.raw_reference
    return RawCaptureResult(
        document=document,
        document_id=capture.document_id,
        document_version_id=capture.document_version_id,
        decision=capture.decision.value,
        connector_type=raw.connector_type if raw is not None else None,
        content_hash=raw.content_hash if raw is not None else None,
        source_artifact=(to_workflow_artifact(raw.source_artifact) if raw is not None else None),
        metadata_artifact=(
            to_workflow_artifact(raw.metadata_artifact) if raw is not None else None
        ),
    )


def to_capture_stage(
    capture: RawCaptureResult,
) -> RawCaptureStageResult:
    raw = None
    if capture.source_artifact is not None:
        if capture.metadata_artifact is None:
            raise HarborInvariantError("capture.metadata_artifact must not be None here")
        if capture.connector_type is None:
            raise HarborInvariantError("capture.connector_type must not be None here")
        if capture.content_hash is None:
            raise HarborInvariantError("capture.content_hash must not be None here")
        raw = RawDocumentReference(
            document_id=DocumentId(capture.document_id),
            connector_type=capture.connector_type,
            content_hash=capture.content_hash,
            source_artifact=to_artifact_reference(capture.source_artifact),
            metadata_artifact=to_artifact_reference(capture.metadata_artifact),
        )
    return RawCaptureStageResult(
        document_id=capture.document_id,
        document_version_id=capture.document_version_id,
        decision=SourceAdmissionDecision(capture.decision),
        raw_reference=raw,
    )


def to_prepared_document(
    document: DocumentIngestionInput,
    prepared: PreparedDocumentStage,
) -> PreparedDocument:
    return PreparedDocument(
        document=document,
        document_id=prepared.document_id,
        document_version_id=prepared.document_version_id,
        decision=prepared.decision.value,
        canonical_reference=(
            to_workflow_artifact(prepared.canonical_reference)
            if prepared.canonical_reference is not None
            else None
        ),
    )


def to_prepared_stage(
    prepared: PreparedDocument,
) -> PreparedDocumentStage:
    return PreparedDocumentStage(
        document_id=prepared.document_id,
        document_version_id=prepared.document_version_id,
        decision=SourceAdmissionDecision(prepared.decision),
        canonical_reference=(
            to_artifact_reference(prepared.canonical_reference)
            if prepared.canonical_reference is not None
            else None
        ),
    )


def to_workflow_artifact(
    reference: ArtifactReference,
) -> WorkflowArtifactReference:
    return WorkflowArtifactReference(
        bucket=reference.bucket,
        key=reference.key,
        sha256=reference.sha256,
        byte_size=reference.byte_size,
        media_type=reference.media_type,
        byte_offset=reference.byte_offset,
        byte_length=reference.byte_length,
    )


def to_artifact_reference(
    reference: WorkflowArtifactReference,
) -> ArtifactReference:
    return ArtifactReference(
        bucket=reference.bucket,
        key=reference.key,
        sha256=reference.sha256,
        byte_size=reference.byte_size,
        media_type=reference.media_type,
        byte_offset=reference.byte_offset,
        byte_length=reference.byte_length,
    )
