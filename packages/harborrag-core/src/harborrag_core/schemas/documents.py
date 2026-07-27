from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import AfterValidator, Field, PlainSerializer, model_validator

from harborrag_core.base import StrictModel, utc_now
from harborrag_core.schemas.ids import (
    ChunkId,
    DataSourceId,
    DocumentId,
    DocumentVersionId,
    TenantId,
)


class DocumentStatus(StrEnum):
    """Enumerates supported document status values."""

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class DocumentRecord(StrictModel):
    """Represents document record data shared across HarborRAG layers."""

    id: DocumentId
    tenant_id: TenantId
    data_source_id: DataSourceId | None = None
    current_version_id: DocumentVersionId
    external_id: str | None = None
    title: str | None = None
    media_type: str | None = None
    content_hash: str
    object_uri: str | None = None
    status: DocumentStatus = DocumentStatus.PENDING
    metadata: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    deleted_at: datetime | None = None


def _freeze_metadata_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenMetadata(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_metadata_value(item) for item in value)
    return value


class FrozenMetadata(Mapping[str, Any]):
    """Recursively immutable mapping with JSON-compatible serialization."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        self._values = {
            str(key): _freeze_metadata_value(value) for key, value in (values or {}).items()
        }

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"FrozenMetadata({self._values!r})"


def thaw_metadata(value: Any) -> Any:
    """Return ordinary JSON containers from recursively frozen metadata."""

    if isinstance(value, Mapping):
        return {str(key): thaw_metadata(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_metadata(item) for item in value]
    return value


ChunkMetadata = Annotated[
    Mapping[str, Any],
    AfterValidator(FrozenMetadata),
    PlainSerializer(thaw_metadata, return_type=dict[str, Any]),
]


def _validate_range(label: str, start: int | None, end: int | None) -> None:
    if (start is None) != (end is None):
        raise ValueError(f"{label} bounds must be provided together")
    if start is not None and end is not None and end < start:
        raise ValueError(f"{label} end must not precede start")


class ChunkSourceSpan(StrictModel):
    """Canonical source location for a chunk revision."""

    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    page_start: int | None = Field(default=None, ge=0)
    page_end: int | None = Field(default=None, ge=0)
    source_element_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_ranges(self) -> ChunkSourceSpan:
        """Reject incomplete, reversed, or blank source locations."""

        _validate_range("offset", self.start_offset, self.end_offset)
        _validate_range("line", self.start_line, self.end_line)
        _validate_range("page", self.page_start, self.page_end)
        if any(not element_id.strip() for element_id in self.source_element_ids):
            raise ValueError("source_element_ids must be non-empty")
        return self


class ChunkContext(StrictModel):
    """Retrieval context kept separate from canonical source content."""

    title: str | None = None
    structural_path: tuple[str, ...] = ()
    parent_title: str | None = None
    previous_chunk_id: ChunkId | None = None
    next_chunk_id: ChunkId | None = None

    @model_validator(mode="after")
    def validate_values(self) -> ChunkContext:
        """Reject blank optional titles and structural path components."""

        if self.title is not None and not self.title.strip():
            raise ValueError("context title must be non-empty when provided")
        if self.parent_title is not None and not self.parent_title.strip():
            raise ValueError("context parent_title must be non-empty when provided")
        if any(not part.strip() for part in self.structural_path):
            raise ValueError("context structural_path parts must be non-empty")
        return self


class ChunkRecord(StrictModel):
    """Canonical immutable chunk revision shared across HarborRAG layers."""

    logical_chunk_id: ChunkId
    chunk_revision_id: ChunkId
    tenant_id: TenantId
    document_id: DocumentId
    document_version_id: DocumentVersionId
    artifact_id: str
    artifact_revision_id: str
    ordinal: int = Field(ge=0)
    role: str = Field(min_length=1)
    content: str
    content_hash: str
    token_count: int | None = Field(default=None, ge=0)
    source_span: ChunkSourceSpan | None = None
    context: ChunkContext = Field(default_factory=ChunkContext)
    metadata: ChunkMetadata = Field(default_factory=FrozenMetadata)
    created_at: datetime | None = None
