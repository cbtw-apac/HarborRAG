from __future__ import annotations

import json
import re
from hashlib import sha256
from math import isfinite

from pydantic import Field, field_validator, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.chunking import (
    ChunkKind,
    CitationLocator,
    ConnectorType,
    DocumentKind,
    RecordKind,
)
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId, TenantId
from harborrag_core.schemas.vector import SparseVector

# Keys a connector produced by normalizing a field's display name, so a dotted
# filter path (``fields.skill_set``) can never be ambiguous.
_PAYLOAD_FIELD_KEY = re.compile(r"[a-z0-9][a-z0-9_]{0,127}")
_MAX_PAYLOAD_FIELDS = 128


class VectorPayload(StrictModel):
    """Minimal evidence payload stored beside a retrieval vector.

    Chunk text has one projection owner: ``content`` in the evidence collection.
    Graph projections contain identifiers and relationships only.
    """

    chunk_id: str = Field(min_length=1)
    logical_chunk_id: str = Field(min_length=1)
    document_id: DocumentId
    document_version_id: DocumentVersionId
    record_kind: RecordKind
    chunk_kind: ChunkKind
    connector_type: ConnectorType
    document_kind: DocumentKind | None = None
    source_scope_id: str = Field(min_length=1)
    source_item_id: str | None = Field(default=None, min_length=1)
    # The item this one is attached to -- an attachment's issue or page -- so a
    # filter that selects parents can reach the documents attached to them.
    parent_source_item_id: str | None = Field(default=None, min_length=1)
    language: str | None = None
    content: str = Field(min_length=1)
    document_title: str | None = None
    section_path: tuple[str, ...] = ()
    token_count: int | None = Field(default=None, ge=0)
    content_hash: str | None = Field(default=None, min_length=1)
    citation_locator: CitationLocator
    quality_score: float = Field(ge=0.0, le=1.0)
    relative_path: str | None = None
    space_id: str | None = None
    page_id: str | None = None
    project_id: str | None = None
    issue_key: str | None = None
    attachment_id: str | None = None
    # Source-owned descriptors a caller narrows or sorts a result set on. They
    # describe the evidence, never replace it: the text stays in ``content``.
    created_at: str | None = None
    updated_at: str | None = None
    resolved_at: str | None = None
    due_date: str | None = None
    status: str | None = None
    status_category: str | None = None
    item_type: str | None = None
    priority: str | None = None
    author: str | None = None
    assignee: str | None = None
    reporter: str | None = None
    creator: str | None = None
    project_key: str | None = None
    project_name: str | None = None
    labels: tuple[str, ...] = ()
    components: tuple[str, ...] = ()
    # The source's own typed fields -- a Jira issue's custom fields -- keyed by
    # normalized field name and filtered as ``fields.<key>``. Values keep their
    # type, so a range filter works on a number and set membership on a list.
    fields: dict[str, str | float | bool | tuple[str, ...]] | None = None
    # Where this chunk sits in the knowledge graph of the same document version:
    # its Chunk node and the source entity the document is a version of. A vector
    # hit joins the graph on these keys directly, without re-deriving a node key
    # from a connector-specific source id ("jira://CPM/CPM-1" vs "CPM-1").
    graph_chunk_node_key: str | None = Field(default=None, min_length=1)
    graph_source_node_key: str | None = Field(default=None, min_length=1)

    @field_validator(
        "language",
        "document_title",
        "source_item_id",
        "content",
        "content_hash",
        "relative_path",
        "space_id",
        "page_id",
        "project_id",
        "issue_key",
        "attachment_id",
        "created_at",
        "updated_at",
        "resolved_at",
        "due_date",
        "status",
        "status_category",
        "item_type",
        "priority",
        "author",
        "assignee",
        "reporter",
        "creator",
        "project_key",
        "project_name",
    )
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("optional vector payload text must be non-empty")
        return value

    @field_validator("fields")
    @classmethod
    def validate_fields(
        cls, value: dict[str, str | float | bool | tuple[str, ...]] | None
    ) -> dict[str, str | float | bool | tuple[str, ...]] | None:
        if value is None:
            return None
        if len(value) > _MAX_PAYLOAD_FIELDS:
            raise ValueError(f"vector payload carries more than {_MAX_PAYLOAD_FIELDS} fields")
        for key, item in value.items():
            if _PAYLOAD_FIELD_KEY.fullmatch(key) is None:
                raise ValueError(f"vector payload field key {key!r} is not a normalized name")
            if isinstance(item, float) and not isfinite(item):
                raise ValueError(f"vector payload field {key!r} must be finite")
            texts = item if isinstance(item, tuple) else (item,) if isinstance(item, str) else ()
            if any(not text.strip() for text in texts):
                raise ValueError(f"vector payload field {key!r} has an empty value")
        return value

    @field_validator("section_path", "labels", "components")
    @classmethod
    def validate_text_sequence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("vector payload sequence entries must be non-empty")
        return value


class _VectorRecord(StrictModel):
    point_id: str = Field(min_length=1)
    tenant_id: TenantId
    dense_vector: tuple[float, ...] = Field(min_length=1)
    sparse_vector: SparseVector
    payload: VectorPayload

    @field_validator("dense_vector")
    @classmethod
    def validate_dense_vector(cls, values: tuple[float, ...]) -> tuple[float, ...]:
        if any(not isfinite(value) for value in values):
            raise ValueError("dense vector values must be finite")
        return values


class VectorEvidenceRecord(_VectorRecord):
    @model_validator(mode="after")
    def validate_evidence(self) -> VectorEvidenceRecord:
        if self.payload.record_kind != RecordKind.EVIDENCE:
            raise ValueError("evidence vector record requires an evidence payload")
        return self


class VectorProjectionManifest(StrictModel):
    schema_version: str = "1.0"
    document_id: DocumentId
    document_version_id: DocumentVersionId
    evidence_point_ids: tuple[str, ...]
    payload_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_point_ids(self) -> VectorProjectionManifest:
        if len(self.evidence_point_ids) != len(set(self.evidence_point_ids)):
            raise ValueError("vector projection point IDs must be unique")
        return self


class VectorProjectionBatch(StrictModel):
    evidence_records: tuple[VectorEvidenceRecord, ...]
    manifest: VectorProjectionManifest

    @model_validator(mode="after")
    def validate_manifest(self) -> VectorProjectionBatch:
        if tuple(record.point_id for record in self.evidence_records) != (
            self.manifest.evidence_point_ids
        ):
            raise ValueError("evidence records do not match the vector projection manifest")
        return self

    @property
    def point_count(self) -> int:
        return len(self.evidence_records)

    @classmethod
    def assemble(
        cls,
        *,
        evidence_records: tuple[VectorEvidenceRecord, ...],
    ) -> VectorProjectionBatch:
        records = evidence_records
        if not records:
            raise ValueError("vector projection batch must not be empty")
        document_ids = {record.payload.document_id for record in records}
        version_ids = {record.payload.document_version_id for record in records}
        if len(document_ids) != 1 or len(version_ids) != 1:
            raise ValueError("vector records must belong to one document version")
        payload_bytes = json.dumps(
            [record.payload.model_dump(mode="json", exclude_none=True) for record in records],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return cls(
            evidence_records=evidence_records,
            manifest=VectorProjectionManifest(
                document_id=next(iter(document_ids)),
                document_version_id=next(iter(version_ids)),
                evidence_point_ids=tuple(record.point_id for record in evidence_records),
                payload_sha256=sha256(payload_bytes).hexdigest(),
            ),
        )
