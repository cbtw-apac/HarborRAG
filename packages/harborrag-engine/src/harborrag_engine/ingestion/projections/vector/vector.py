from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.chunking import ChunkRecord, RecordKind
from harborrag_core.ingestion import (
    ChunkSetArtifacts,
    ContentReference,
    DocumentIdentityBuilder,
    RepresentationSet,
    VectorEvidenceRecord,
    VectorPayload,
    VectorProjectionBatch,
    VectorRouteRecord,
    reject_runtime_fields,
)

ROUTE_INDEX = "routes"
EVIDENCE_INDEX = "evidence"


@dataclass(frozen=True, slots=True)
class VectorProjectionInput:
    chunks: tuple[ChunkRecord, ...]
    representations: RepresentationSet
    chunk_artifacts: ChunkSetArtifacts


class VectorProjectionBuilder:
    """Build versioned Qdrant records without performing external writes."""

    def __init__(
        self,
        *,
        preview_characters: int = 320,
    ) -> None:
        if preview_characters < 1:
            raise ValueError("preview_characters must be positive")
        self._preview_characters = preview_characters
        self._identity = DocumentIdentityBuilder()

    def build(self, request: VectorProjectionInput) -> VectorProjectionBatch:
        if not request.chunks:
            raise ValueError("vector projection requires canonical chunks")
        entries = {entry.chunk_id: entry for entry in request.chunk_artifacts.entries}
        representations = {record.chunk_id: record for record in request.representations.records}
        first = request.chunks[0]
        if str(request.representations.document_id) != str(first.document_id) or str(
            request.representations.document_version_id
        ) != str(first.document_version_id):
            raise ValueError("representation set belongs to another document version")
        route_records: list[VectorRouteRecord] = []
        evidence_records: list[VectorEvidenceRecord] = []
        for chunk in request.chunks:
            chunk_id = str(chunk.chunk_id)
            representation = representations.get(chunk_id)
            if representation is None:
                raise ValueError(f"representation is missing for chunk {chunk_id}")
            entry = entries.get(chunk_id)
            if entry is None:
                raise ValueError(f"content reference is missing for chunk {chunk_id}")
            content_reference = request.chunk_artifacts.content_reference(chunk_id)
            payload = self._payload(
                chunk,
                content_reference=content_reference,
            )
            record_type = (
                VectorRouteRecord if chunk.record_kind == RecordKind.ROUTE else VectorEvidenceRecord
            )
            record = record_type(
                point_id=self._identity.point_id(chunk_id=chunk_id),
                tenant_id=chunk.tenant_id,
                dense_vector=tuple(representation.dense_vector),
                sparse_vector=representation.sparse_vector,
                payload=payload,
            )
            # Narrow on the record itself rather than re-testing record_kind: that keeps the
            # chosen list and the constructed type provably in step.
            if isinstance(record, VectorRouteRecord):
                route_records.append(record)
            else:
                evidence_records.append(record)
        return VectorProjectionBatch.assemble(
            route_records=tuple(route_records),
            evidence_records=tuple(evidence_records),
        )

    def _payload(
        self,
        chunk: ChunkRecord,
        *,
        content_reference: ContentReference,
    ) -> VectorPayload:
        metadata = chunk.metadata
        payload: dict[str, object] = {
            "chunk_id": str(chunk.chunk_id),
            "logical_chunk_id": str(chunk.logical_chunk_id),
            "document_id": str(chunk.document_id),
            "document_version_id": str(chunk.document_version_id),
            "record_kind": chunk.record_kind.value,
            "chunk_kind": chunk.chunk_kind.value,
            "connector_type": chunk.connector_type.value,
            "document_kind": chunk.document_kind.value,
            "source_scope_id": chunk.source_scope_id,
            "source_item_id": chunk.source_item_id,
            "language": chunk.language,
            "content_reference": content_reference,
            "content": chunk.content,
            "preview": chunk.content[: self._preview_characters],
            "document_title": chunk.hierarchy.document_title,
            "section_path": chunk.hierarchy.section_path,
            "token_count": chunk.token_count,
            "content_hash": chunk.content_hash,
            "citation_locator": chunk.citation_locator,
            "quality_score": chunk.quality.score,
        }
        for field in (
            "relative_path",
            "space_id",
            "page_id",
            "project_id",
            "issue_key",
            "attachment_id",
        ):
            value = metadata.get(field)
            if isinstance(value, (str, int)):
                payload[field] = str(value)
        reject_runtime_fields(payload)
        return VectorPayload.model_validate(payload)
