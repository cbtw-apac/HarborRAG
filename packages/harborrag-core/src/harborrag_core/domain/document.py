"""Canonical document, block, and relationship domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.chunking.metadata import ChunkMetadata, FrozenMetadata
from harborrag_core.chunking.source_schemas import SourceLocator

from .element import DocumentElement
from .provenance import DocumentProvenance
from .table import TableArtifact
from .validation import require_id


class DocumentBlockKind(StrEnum):
    DOCUMENT = "document"
    SECTION = "section"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    LIST_ITEM = "list_item"
    CODE_BLOCK = "code_block"
    QUOTE = "quote"
    PANEL = "panel"
    EXPAND = "expand"
    TAB_SET = "tab_set"
    TAB = "tab"
    MACRO = "macro"
    TABLE_REFERENCE = "table_reference"
    MEDIA_REFERENCE = "media_reference"
    LINK_REFERENCE = "link_reference"
    UNSUPPORTED = "unsupported"


class DocumentBlock(StrictModel):
    """One ordered node in a canonical document block tree."""

    block_id: str = Field(min_length=1)
    kind: DocumentBlockKind
    ordinal: int = Field(ge=0)
    text: str | None = None
    title: str | None = None
    heading_level: int | None = Field(default=None, ge=1, le=6)
    parent_block_id: str | None = None
    source_block_id: str | None = None
    source_locator: SourceLocator = Field(default_factory=SourceLocator)
    section_id: str | None = None
    parent_section_id: str | None = None
    section_path: tuple[str, ...] = ()
    tab_path: tuple[str, ...] = ()
    container_path: tuple[str, ...] = ()
    attributes: ChunkMetadata = Field(default_factory=FrozenMetadata)
    children: tuple[DocumentBlock, ...] = ()

    @field_validator(
        "block_id",
        "parent_block_id",
        "source_block_id",
        "section_id",
        "parent_section_id",
    )
    @classmethod
    def validate_identifiers(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("canonical block identifiers must be non-empty")
        return value

    @model_validator(mode="after")
    def validate_structure(self) -> DocumentBlock:
        if self.kind == DocumentBlockKind.HEADING and self.heading_level is None:
            raise ValueError("heading blocks require heading_level")
        if self.kind != DocumentBlockKind.HEADING and self.heading_level is not None:
            raise ValueError("heading_level is only valid for heading blocks")
        if any(
            not part.strip() for part in (*self.section_path, *self.tab_path, *self.container_path)
        ):
            raise ValueError("canonical block paths must contain non-empty values")
        child_ids = tuple(child.block_id for child in self.children)
        if len(set(child_ids)) != len(child_ids):
            raise ValueError("canonical block child IDs must be unique")
        if any(child.parent_block_id != self.block_id for child in self.children):
            raise ValueError("canonical block children must reference their parent")
        ordinals = tuple(child.ordinal for child in self.children)
        if list(ordinals) != sorted(set(ordinals)):
            raise ValueError("canonical block children must have unique ascending ordinals")
        return self

    @property
    def ordered_child_block_ids(self) -> tuple[str, ...]:
        return tuple(child.block_id for child in self.children)


@dataclass(slots=True, kw_only=True)
class DocumentRelation:
    """A source-derived structural edge from this document to another entity."""

    predicate: str = field(
        metadata={
            "description": "Relationship type, such as parent_of, child_of, or has_attachment"
        }
    )
    target_id: str = field(
        metadata={"description": "Target document or entity ID, such as confluence://SPACE/123"}
    )
    target_type: str = field(
        metadata={"description": "Target entity type, such as document, person, tag, or space"}
    )
    metadata: dict[str, Any] = field(
        default_factory=dict,
        metadata={"description": "Additional source-derived relationship metadata"},
    )


@dataclass(slots=True, kw_only=True)
class Document:
    """Canonical normalized document passed between ingestion stages."""

    id: str = field(
        metadata={"description": "Globally unique document ID, e.g. confluence://SPACE/123"}
    )
    title: str = field(metadata={"description": "Human-readable title of the document"})
    content: list[DocumentElement] = field(
        metadata={
            "description": "Structured document elements such as headings, paragraphs, or tables"
        }
    )
    content_type: str = field(
        metadata={"description": "Source content type, such as page, comment, or attachment"}
    )
    provenance: DocumentProvenance = field(
        metadata={"description": "Origin, access, and source-timestamp information"}
    )
    relations: list[DocumentRelation] = field(
        default_factory=list,
        metadata={"description": "Source-derived structural edges from this document"},
    )
    raw: dict[str, Any] | None = field(
        default=None,
        metadata={"description": "Optional raw source data retained for diagnostics"},
    )
    blocks: tuple[DocumentBlock, ...] = field(
        default_factory=tuple,
        metadata={"description": "Ordered canonical document block roots"},
    )
    table_artifacts: tuple[TableArtifact, ...] = field(
        default_factory=tuple,
        metadata={"description": "Structured table artifacts referenced by canonical blocks"},
    )
    body_representation: str | None = field(
        default=None,
        metadata={"description": "Structured source representation selected during normalization"},
    )
    warnings: tuple[str, ...] = field(
        default_factory=tuple,
        metadata={"description": "Recoverable parser and normalization warnings"},
    )

    def __post_init__(self) -> None:
        require_id(self.id, label="Document")
        table_ids = [artifact.table_id for artifact in self.table_artifacts]
        if len(set(table_ids)) != len(table_ids):
            raise ValueError("Document table_artifacts must have unique table_id values")
        if any(artifact.document_id != self.id for artifact in self.table_artifacts):
            raise ValueError("Document table_artifacts must reference this document id")
