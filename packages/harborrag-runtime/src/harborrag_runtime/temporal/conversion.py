from __future__ import annotations

import json
from datetime import datetime

from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import (
    ArtifactReference,
    ProcessingProfile,
    RawDocumentReference,
    SourceAdmissionDecision,
)
from harborrag_core.invariants import HarborInvariantError
from harborrag_core.schemas.ids import DocumentId
from harborrag_runtime.ingestion.document.stage_models import (
    PreparedDocumentStage,
    RawCaptureStageResult,
)
from harborrag_runtime.ingestion.source.models import SourceIngestionRequest

from .schemas import (
    DocumentIngestionInput,
    PreparedDocument,
    ProcessingProfileInput,
    RawCaptureResult,
    SourceIngestionInput,
    WorkflowArtifactReference,
)


def to_source_request(source: SourceIngestionInput) -> SourceIngestionRequest:
    query = source.query
    updated_after = (
        datetime.fromisoformat(query.updated_after) if query.updated_after is not None else None
    )
    return SourceIngestionRequest(
        tenant_id=source.tenant_id,
        task_id=source.task_id,
        connector_name=source.connector_name,
        connector_type=ConnectorType(source.connector_type),
        connection_id=source.connection_id,
        source_scope_id=source.source_scope_id,
        configuration_fingerprint=source.configuration_fingerprint,
        processing=to_processing_profile(source.processing),
        query=ConnectorQuery(
            path=query.path,
            pattern=query.pattern,
            recursive=query.recursive,
            updated_after=updated_after,
            limit=query.limit,
            include_attachments=query.include_attachments,
            filters=json.loads(query.filters_json),
        ),
        force_reprocess=source.force_reprocess,
        discovery_page_size=source.discovery_page_size,
        discovery_concurrency=source.discovery_concurrency,
        document_concurrency=source.document_concurrency,
        missing_threshold=source.missing_threshold,
    )


def to_processing_profile(
    processing: ProcessingProfileInput,
) -> ProcessingProfile:
    return ProcessingProfile(
        parser_profile=processing.parser_profile,
        normalizer_version=processing.normalizer_version,
        chunk_strategy=processing.chunk_strategy,
        dense_encoder_profile=processing.dense_encoder_profile,
        sparse_encoder_profile=processing.sparse_encoder_profile,
        graph_projection_version=processing.graph_projection_version,
        vector_projection_schema=processing.vector_projection_schema,
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
