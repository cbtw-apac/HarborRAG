from __future__ import annotations

import json
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

from .artifact_contracts import ContentReference


class VectorPayload(StrictModel):
    """Reviewable retrieval payload shared by route and evidence projections.

    ``content_reference`` remains the immutable source of truth used by runtime
    retrieval.  The display fields intentionally live beside the vectors so an
    operator can inspect and diagnose an index without access to object storage.
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
    language: str | None = None
    content_reference: ContentReference
    content: str | None = Field(default=None, min_length=1)
    preview: str = Field(min_length=1)
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
    )
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("optional vector payload text must be non-empty")
        return value

    @field_validator("section_path")
    @classmethod
    def validate_section_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("vector payload section path entries must be non-empty")
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


class VectorRouteRecord(_VectorRecord):
    @model_validator(mode="after")
    def validate_route(self) -> VectorRouteRecord:
        if self.payload.record_kind != RecordKind.ROUTE:
            raise ValueError("route vector record requires a route payload")
        return self


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
    route_point_ids: tuple[str, ...]
    evidence_point_ids: tuple[str, ...]
    payload_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_point_ids(self) -> VectorProjectionManifest:
        point_ids = (*self.route_point_ids, *self.evidence_point_ids)
        if len(point_ids) != len(set(point_ids)):
            raise ValueError("vector projection point IDs must be unique")
        return self


class VectorProjectionBatch(StrictModel):
    route_records: tuple[VectorRouteRecord, ...]
    evidence_records: tuple[VectorEvidenceRecord, ...]
    manifest: VectorProjectionManifest

    @model_validator(mode="after")
    def validate_manifest(self) -> VectorProjectionBatch:
        if tuple(record.point_id for record in self.route_records) != (
            self.manifest.route_point_ids
        ):
            raise ValueError("route records do not match the vector projection manifest")
        if tuple(record.point_id for record in self.evidence_records) != (
            self.manifest.evidence_point_ids
        ):
            raise ValueError("evidence records do not match the vector projection manifest")
        return self

    @property
    def point_count(self) -> int:
        return len(self.route_records) + len(self.evidence_records)

    @classmethod
    def assemble(
        cls,
        *,
        route_records: tuple[VectorRouteRecord, ...],
        evidence_records: tuple[VectorEvidenceRecord, ...],
    ) -> VectorProjectionBatch:
        records = (*route_records, *evidence_records)
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
            route_records=route_records,
            evidence_records=evidence_records,
            manifest=VectorProjectionManifest(
                document_id=next(iter(document_ids)),
                document_version_id=next(iter(version_ids)),
                route_point_ids=tuple(record.point_id for record in route_records),
                evidence_point_ids=tuple(record.point_id for record in evidence_records),
                payload_sha256=sha256(payload_bytes).hexdigest(),
            ),
        )
