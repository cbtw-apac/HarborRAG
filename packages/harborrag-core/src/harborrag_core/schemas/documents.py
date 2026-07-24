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


class FrozenMetadata(Mapping[str, Any]):
    """Recursively immutable mapping with JSON-compatible serialization."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        self._values = {
            str(key): _freeze_metadata_value(value)
            for key, value in (values or {}).items()
        }

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"FrozenMetadata({self._values!r})"


def _freeze_metadata_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenMetadata(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_metadata_value(item) for item in value)
    return value


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
    """Canonical immutable chunk revision shared across HarborRAG layers.

    ``id`` remains the storage-facing alias of ``chunk_revision_id`` for
    compatibility with existing repositories. Logical identity is stable across
    content changes; revision identity is content/configuration specific.
    """

    id: ChunkId
    logical_chunk_id: ChunkId
    chunk_revision_id: ChunkId
    tenant_id: TenantId
    document_id: DocumentId
    document_version_id: DocumentVersionId
    artifact_id: str
    artifact_revision_id: str
    chunk_index: int = Field(ge=0)
    ordinal: int = Field(ge=0)
    role: str = Field(min_length=1)
    content: str
    content_hash: str
    token_count: int | None = Field(default=None, ge=0)
    source_span: ChunkSourceSpan | None = None
    context: ChunkContext = Field(default_factory=ChunkContext)
    metadata: ChunkMetadata = Field(default_factory=FrozenMetadata)

    # Flat compatibility fields retained for current repositories and callers.
    structural_path: tuple[str, ...] = ()
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    page_start: int | None = Field(default=None, ge=0)
    page_end: int | None = Field(default=None, ge=0)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    parent_chunk_id: ChunkId | None = None
    previous_chunk_id: ChunkId | None = None
    next_chunk_id: ChunkId | None = None
    source_element_ids: tuple[str, ...] = ()
    created_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def fill_legacy_identity_fields(cls, value: Any) -> Any:
        """Upgrade older repository-shaped records into canonical records."""

        if not isinstance(value, Mapping):
            return value
        values = dict(value)
        storage_id = values.get("id") or values.get("chunk_revision_id")
        if storage_id is None:
            return values
        revision_id = values.setdefault("chunk_revision_id", storage_id)
        if str(storage_id) != str(revision_id):
            raise ValueError("id must equal chunk_revision_id")
        values.setdefault("id", revision_id)
        values.setdefault("logical_chunk_id", storage_id)
        values.setdefault("artifact_id", values.get("document_id"))
        values.setdefault("artifact_revision_id", values.get("document_version_id"))
        values.setdefault("ordinal", values.get("chunk_index", 0))
        values.setdefault("role", "content")
        source_span = values.get("source_span")
        if source_span is None and (
            any(
                values.get(key) is not None
                for key in (
                    "start_offset",
                    "end_offset",
                    "start_line",
                    "end_line",
                    "page_start",
                    "page_end",
                )
            )
            or values.get("source_element_ids")
        ):
            values["source_span"] = {
                "start_offset": values.get("start_offset"),
                "end_offset": values.get("end_offset"),
                "start_line": values.get("start_line"),
                "end_line": values.get("end_line"),
                "page_start": values.get("page_start"),
                "page_end": values.get("page_end"),
                "source_element_ids": values.get("source_element_ids", ()),
            }
        elif source_span is not None:
            span_values = (
                source_span.model_dump()
                if isinstance(source_span, ChunkSourceSpan)
                else dict(source_span)
            )
            values.setdefault("start_offset", span_values.get("start_offset"))
            values.setdefault("end_offset", span_values.get("end_offset"))
            values.setdefault("start_line", span_values.get("start_line"))
            values.setdefault("end_line", span_values.get("end_line"))
            values.setdefault("page_start", span_values.get("page_start"))
            values.setdefault("page_end", span_values.get("page_end"))
            values.setdefault(
                "source_element_ids", span_values.get("source_element_ids", ())
            )

        context = values.get("context")
        if context is None:
            values["context"] = {
                "structural_path": values.get("structural_path", ()),
                "previous_chunk_id": values.get("previous_chunk_id"),
                "next_chunk_id": values.get("next_chunk_id"),
            }
        else:
            context_values = (
                context.model_dump()
                if isinstance(context, ChunkContext)
                else dict(context)
            )
            values.setdefault(
                "structural_path", context_values.get("structural_path", ())
            )
            values.setdefault(
                "previous_chunk_id", context_values.get("previous_chunk_id")
            )
            values.setdefault("next_chunk_id", context_values.get("next_chunk_id"))
        return values

    @model_validator(mode="after")
    def validate_ranges(self) -> ChunkRecord:
        """Require canonical and compatibility location fields to agree."""
        _validate_range("offset", self.start_offset, self.end_offset)
        _validate_range("page", self.page_start, self.page_end)
        _validate_range("line", self.start_line, self.end_line)
        if any(not part.strip() for part in self.structural_path):
            raise ValueError("structural_path parts must be non-empty")
        if any(not element_id.strip() for element_id in self.source_element_ids):
            raise ValueError("source_element_ids must be non-empty")
        _validate_source_span(self)
        _validate_context(self)
        return self


def _validate_range(label: str, start: int | None, end: int | None) -> None:
    if (start is None) != (end is None):
        raise ValueError(f"{label} bounds must be provided together")
    if start is not None and end is not None and end < start:
        raise ValueError(f"{label} end must not precede start")


def _validate_source_span(chunk: ChunkRecord) -> None:
    span = chunk.source_span
    if span is None:
        return
    expected = {
        "start_offset": chunk.start_offset,
        "end_offset": chunk.end_offset,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "source_element_ids": chunk.source_element_ids,
    }
    for name, value in expected.items():
        if getattr(span, name) != value:
            label = "element IDs" if name == "source_element_ids" else name
            raise ValueError(f"source_span {label} does not match flat field")


def _validate_context(chunk: ChunkRecord) -> None:
    expected = {
        "structural_path": chunk.structural_path,
        "previous_chunk_id": chunk.previous_chunk_id,
        "next_chunk_id": chunk.next_chunk_id,
    }
    for name, value in expected.items():
        if getattr(chunk.context, name) != value:
            raise ValueError(f"context {name} does not match flat field")
