from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from harborrag_core.chunking import ChunkRecord, RecordKind
from harborrag_core.ingestion import (
    ChunkSetArtifacts,
    DocumentIdentityBuilder,
    RepresentationSet,
    VectorEvidenceRecord,
    VectorPayload,
    VectorProjectionBatch,
    reject_runtime_fields,
)

EVIDENCE_INDEX = "evidence"


@dataclass(frozen=True, slots=True)
class VectorProjectionInput:
    chunks: tuple[ChunkRecord, ...]
    representations: RepresentationSet
    chunk_artifacts: ChunkSetArtifacts


class VectorProjectionBuilder:
    """Build the single evidence projection without external writes."""

    def __init__(self) -> None:
        self._identity = DocumentIdentityBuilder()

    def build(self, request: VectorProjectionInput) -> VectorProjectionBatch:
        if not request.chunks:
            raise ValueError("vector projection requires canonical chunks")
        representations = {record.chunk_id: record for record in request.representations.records}
        first = request.chunks[0]
        if str(request.representations.document_id) != str(first.document_id) or str(
            request.representations.document_version_id
        ) != str(first.document_version_id):
            raise ValueError("representation set belongs to another document version")
        evidence_records: list[VectorEvidenceRecord] = []
        for chunk in request.chunks:
            if chunk.record_kind != RecordKind.EVIDENCE:
                continue
            chunk_id = str(chunk.chunk_id)
            representation = representations.get(chunk_id)
            if representation is None:
                raise ValueError(f"representation is missing for chunk {chunk_id}")
            payload = self._payload(chunk)
            record = VectorEvidenceRecord(
                point_id=self._identity.point_id(chunk_id=chunk_id),
                tenant_id=chunk.tenant_id,
                dense_vector=tuple(representation.dense_vector),
                sparse_vector=representation.sparse_vector,
                payload=payload,
            )
            evidence_records.append(record)
        return VectorProjectionBatch.assemble(evidence_records=tuple(evidence_records))

    def _payload(
        self,
        chunk: ChunkRecord,
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
            "content": chunk.content,
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
            "status",
            "status_category",
            "priority",
            "author",
            "assignee",
            "reporter",
            "creator",
            "project_key",
            "project_name",
            "resolved_at",
            "due_date",
        ):
            value = metadata.get(field)
            if isinstance(value, (str, int)):
                payload[field] = str(value)
        # A chunk's own timestamps where the source has them -- a comment or an
        # attachment is written and edited on its own clock -- and the document's
        # otherwise, so every point can be narrowed or sorted by time.
        for field, fallback in (
            ("created_at", "source_created_at"),
            ("updated_at", "source_updated_at"),
        ):
            value = metadata.get(field) or metadata.get(fallback)
            if isinstance(value, (str, int)):
                payload[field] = str(value)
        # ``issue_type`` for Jira, ``content_type`` elsewhere: one connector-neutral
        # name, because a caller filtering by kind should not branch per connector.
        parent = metadata.get("parent_source_item_id")
        if isinstance(parent, str) and parent.strip():
            payload["parent_source_item_id"] = parent
        item_type = metadata.get("issue_type") or metadata.get("item_type")
        if isinstance(item_type, (str, int)):
            payload["item_type"] = str(item_type)
        for field in ("labels", "components"):
            payload[field] = self._text_sequence(metadata.get(field))
        fields = metadata.get("fields")
        if isinstance(fields, Mapping) and fields:
            payload["fields"] = dict(fields)
        reject_runtime_fields(payload)
        return VectorPayload.model_validate(payload)

    @staticmethod
    def _text_sequence(value: object) -> tuple[str, ...]:
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            return ()
        return tuple(text for item in value if item is not None and (text := str(item).strip()))
