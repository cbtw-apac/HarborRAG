"""Explicit JSON codecs for durable ingestion artifacts."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from harborrag_core.domain.document import Document, DocumentRelation
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_core.schemas.documents import ChunkRecord
from harborrag_engine.ingestion.chunking.schemas import (
    ChunkingDiagnostics,
    ChunkingResult,
    ChunkManifest,
    ChunkReference,
    ChunkValidationResult,
)
from harborrag_engine.ingestion.indexing.schemas import (
    GenerationActivationPlan,
    IndexingDiagnostics,
    IndexingResult,
    IndexingStatus,
)
from pydantic_core import to_jsonable_python

from harborrag_runtime.temporal.models import (
    ArtifactActivityResult,
    ArtifactReference,
    ArtifactStage,
    ArtifactStageState,
    ArtifactStatus,
    DiscoveryResult,
    PendingResolution,
)

CODEC_VERSION = 1


def dump_payload(kind: str, value: Any) -> bytes:
    envelope = {
        "version": CODEC_VERSION,
        "kind": kind,
        "value": to_jsonable_python(value),
    }
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")


def load_payload(payload: bytes, kind: str) -> Any:
    envelope = json.loads(payload)
    if envelope.get("version") != CODEC_VERSION or envelope.get("kind") != kind:
        raise ValueError(f"unsupported or invalid {kind!r} ingestion payload")
    return envelope["value"]


def dump_raw_document(document: RawDocument) -> bytes:
    content = document.content
    encoded = (
        {"encoding": "text", "value": content}
        if isinstance(content, str)
        else {
            "encoding": "base64",
            "value": base64.b64encode(content).decode("ascii"),
        }
    )
    return dump_payload(
        "raw-document",
        {
            "id": document.id,
            "source": document.source,
            "content": encoded,
            "content_type": document.content_type,
            "metadata": document.metadata,
            "raw": document.raw,
        },
    )


def load_raw_document(payload: bytes) -> RawDocument:
    value = load_payload(payload, "raw-document")
    content = value["content"]
    body = content["value"] if content["encoding"] == "text" else base64.b64decode(content["value"])
    return RawDocument(
        id=value["id"],
        source=value["source"],
        content=body,
        content_type=value["content_type"],
        metadata=dict(value.get("metadata") or {}),
        raw=value.get("raw"),
    )


def load_source_record(payload: bytes) -> SourceRecord:
    value = load_payload(payload, "source-record")
    return SourceRecord(
        id=value["id"],
        source_type=value["source_type"],
        locator=value["locator"],
        metadata=dict(value.get("metadata") or {}),
        updated_at=_datetime(value.get("updated_at")),
        checksum=value.get("checksum"),
    )


def dump_parsed_document(parsed: ParsedDocument, document: Document) -> bytes:
    return dump_payload("parsed-document", {"parsed": parsed, "document": document})


def load_document(payload: bytes) -> Document:
    value = load_payload(payload, "parsed-document")["document"]
    provenance = value["provenance"]
    return Document(
        id=value["id"],
        title=value["title"],
        content=[DocumentElement(**item) for item in value.get("content", ())],
        content_type=value["content_type"],
        provenance=DocumentProvenance(
            source=provenance["source"],
            record_id=provenance.get("record_id"),
            url=provenance.get("url"),
            author=provenance.get("author"),
            checksum=provenance.get("checksum"),
            permissions=dict(provenance.get("permissions") or {}),
            created_at=_datetime(provenance.get("created_at")),
            updated_at=_datetime(provenance.get("updated_at")),
            tags=list(provenance.get("tags") or ()),
            extra=dict(provenance.get("extra") or {}),
        ),
        relations=[DocumentRelation(**item) for item in value.get("relations", ())],
        raw=value.get("raw"),
    )


def load_chunk_manifest(value: dict[str, Any]) -> ChunkManifest:
    return ChunkManifest(
        tenant_id=value["tenant_id"],
        artifact_id=value["artifact_id"],
        artifact_revision_id=value["artifact_revision_id"],
        chunker_name=value["chunker_name"],
        chunker_version=value["chunker_version"],
        configuration_hash=value["configuration_hash"],
        chunks=tuple(ChunkReference(**item) for item in value.get("chunks", ())),
        total_token_count=value["total_token_count"],
        total_chunk_count=value["total_chunk_count"],
        validation=ChunkValidationResult(
            valid=value["validation"]["valid"],
            errors=tuple(value["validation"].get("errors", ())),
            warnings=tuple(value["validation"].get("warnings", ())),
        ),
        fingerprint=value["fingerprint"],
    )


def load_chunking_result(payload: bytes) -> ChunkingResult:
    value = load_payload(payload, "chunking-result")
    return ChunkingResult(
        artifact_id=value["artifact_id"],
        artifact_revision_id=value["artifact_revision_id"],
        strategy=value["strategy"],
        profile=value["profile"],
        profile_hash=value["profile_hash"],
        chunks=tuple(ChunkRecord.model_validate(item) for item in value.get("chunks", ())),
        diagnostics=ChunkingDiagnostics(**value["diagnostics"]),
        manifest=load_chunk_manifest(value["manifest"]),
    )


def load_indexing_result(payload: bytes) -> IndexingResult:
    value = load_payload(payload, "indexing-result")
    activation = value["activation"]
    return IndexingResult(
        artifact_id=value["artifact_id"],
        artifact_revision_id=value["artifact_revision_id"],
        generation_id=value["generation_id"],
        status=IndexingStatus(value["status"]),
        vector_valid=value["vector_valid"],
        graph_valid=value["graph_valid"],
        validation_errors=tuple(value.get("validation_errors", ())),
        diagnostics=IndexingDiagnostics(**value["diagnostics"]),
        activation=GenerationActivationPlan(
            artifact_id=activation["artifact_id"],
            generation_id=activation["generation_id"],
            previous_generation_id=activation.get("previous_generation_id"),
            vector_collection=activation["vector_collection"],
            activate_vector_ids=tuple(activation.get("activate_vector_ids", ())),
            retire_vector_ids=tuple(activation.get("retire_vector_ids", ())),
            delete_vector_ids=tuple(activation.get("delete_vector_ids", ())),
            tombstone_vector_ids=tuple(activation.get("tombstone_vector_ids", ())),
        ),
    )


def load_discovery_result(value: dict[str, Any]) -> DiscoveryResult:
    return DiscoveryResult(
        artifacts=tuple(ArtifactReference(**item) for item in value.get("artifacts", ())),
        next_cursor=value.get("next_cursor"),
        checkpoint_ref=value["checkpoint_ref"],
        done=value["done"],
        version=value.get("version", 1),
    )


def load_activity_result(value: dict[str, Any]) -> ArtifactActivityResult:
    state = value["state"]
    artifact = state["artifact"]
    pending = value.get("pending_resolution")
    return ArtifactActivityResult(
        status=ArtifactStatus(value["status"]),
        state=ArtifactStageState(
            artifact=ArtifactReference(**artifact),
            generation_id=state["generation_id"],
            stage=ArtifactStage(state["stage"]),
            artifact_revision_id=state.get("artifact_revision_id"),
            snapshot_ref=state.get("snapshot_ref"),
            parsed_document_ref=state.get("parsed_document_ref"),
            chunking_result_ref=state.get("chunking_result_ref"),
            indexing_result_ref=state.get("indexing_result_ref"),
            checkpoint_ref=state.get("checkpoint_ref"),
            version=state.get("version", 1),
        ),
        pending_resolution=(
            PendingResolution(
                artifact_id=pending["artifact_id"],
                request_ref=pending["request_ref"],
                reason=pending["reason"],
                resume_stage=ArtifactStage(pending["resume_stage"]),
                workflow_id=pending.get("workflow_id"),
                version=pending.get("version", 1),
            )
            if pending
            else None
        ),
        error_type=value.get("error_type"),
        error_message=value.get("error_message"),
        retryable=value.get("retryable", False),
        version=value.get("version", 1),
    )


def _datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
