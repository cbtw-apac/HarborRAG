from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.chunking.metadata import ChunkMetadata, FrozenMetadata
from harborrag_core.chunking.source_schemas import SourceLocator


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
        if any(not part.strip() for part in (*self.section_path, *self.tab_path)):
            raise ValueError("canonical block paths must contain non-empty values")
        child_ids = tuple(child.block_id for child in self.children)
        if len(set(child_ids)) != len(child_ids):
            raise ValueError("canonical block child IDs must be unique")
        if any(child.parent_block_id != self.block_id for child in self.children):
            raise ValueError("canonical block children must reference their parent")
        return self

    @property
    def ordered_child_block_ids(self) -> tuple[str, ...]:
        return tuple(child.block_id for child in self.children)


ContainerBlock = DocumentBlock
