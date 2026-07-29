from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.schemas.ids import ChunkId

from .source_schemas import SourceLocator


class ConnectorType(StrEnum):
    CONFLUENCE = "confluence"
    JIRA = "jira"
    LOCAL = "local"


class DocumentKind(StrEnum):
    CONFLUENCE_PAGE = "confluence_page"
    JIRA_ISSUE = "jira_issue"
    ATTACHMENT = "attachment"
    LOCAL_FILE = "local_file"


class ChunkKind(StrEnum):
    ROUTE = "route"
    EVIDENCE = "evidence"
    TABLE = "table"
    CODE = "code"
    COMMENT = "comment"
    EVENT = "event"
    JIRA_FIELD = "jira_field"


class ContainerKind(StrEnum):
    SECTION = "section"
    TAB_SET = "tab_set"
    TAB = "tab"
    EXPAND = "expand"
    PANEL = "panel"
    MACRO = "macro"


class RelationType(StrEnum):
    CHILD_OF = "child_of"
    LINKS_TO = "links_to"
    HAS_ATTACHMENT = "has_attachment"
    ATTACHED_TO = "attached_to"
    BLOCKS = "blocks"
    DUPLICATES = "duplicates"
    RELATES_TO = "relates_to"
    MENTIONS = "mentions"


class ChunkContainer(StrictModel):
    """One ordered structural container surrounding a chunk."""

    container_id: str = Field(min_length=1)
    kind: ContainerKind
    ordinal: int = Field(ge=0)
    title: str | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("container title must be non-empty when provided")
        return value


class ChunkHierarchy(StrictModel):
    """Ordered ancestry and neighboring chunk references."""

    document_title: str | None = None
    section_path: tuple[str, ...] = ()
    section_id: str | None = None
    parent_section_id: str | None = None
    ancestry: tuple[str, ...] = ()
    containers: tuple[ChunkContainer, ...] = ()
    parent_title: str | None = None
    parent_chunk_id: ChunkId | None = None
    previous_chunk_id: ChunkId | None = None
    next_chunk_id: ChunkId | None = None

    @model_validator(mode="after")
    def validate_hierarchy(self) -> ChunkHierarchy:
        values = (
            *self.section_path,
            *self.ancestry,
            *((self.section_id,) if self.section_id is not None else ()),
            *((self.parent_section_id,) if self.parent_section_id is not None else ()),
        )
        if any(not value.strip() for value in values):
            raise ValueError("chunk hierarchy identifiers and section path must be non-empty")
        if self.document_title is not None and not self.document_title.strip():
            raise ValueError("document_title must be non-empty when provided")
        if self.parent_title is not None and not self.parent_title.strip():
            raise ValueError("parent_title must be non-empty when provided")
        if len(set(self.ancestry)) != len(self.ancestry):
            raise ValueError("chunk hierarchy ancestry must not contain duplicates")
        if self.section_id is not None and self.section_id in self.ancestry:
            raise ValueError("chunk hierarchy ancestry must not contain its section_id")
        if (
            self.parent_section_id is not None
            and self.ancestry
            and self.parent_section_id != self.ancestry[-1]
        ):
            raise ValueError("parent_section_id must be the last ancestry entry")
        if self.previous_chunk_id is not None and self.previous_chunk_id == self.next_chunk_id:
            raise ValueError("previous_chunk_id and next_chunk_id must differ")
        return self


class ChunkRelation(StrictModel):
    """A typed relation from a chunk to another stable source entity."""

    relation_type: RelationType
    target_id: str = Field(min_length=1)
    target_version_id: str | None = None

    @field_validator("target_id", "target_version_id")
    @classmethod
    def validate_target(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("relation target identifiers must be non-empty")
        return value


class ChunkQuality(StrictModel):
    """Bounded quality assessment attached to a chunk."""

    score: float = Field(default=1.0, ge=0.0, le=1.0)
    is_complete: bool = True
    issues: tuple[str, ...] = ()

    @field_validator("issues")
    @classmethod
    def validate_issues(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("chunk quality issues must be non-empty")
        if len(set(values)) != len(values):
            raise ValueError("chunk quality issues must not contain duplicates")
        return values


class ChunkSourceSpan(SourceLocator):
    """Compatibility wrapper for the former source-span contract."""


class ChunkContext(StrictModel):
    """Compatibility view of the former retrieval context contract."""

    title: str | None = None
    structural_path: tuple[str, ...] = ()
    parent_title: str | None = None
    previous_chunk_id: ChunkId | None = None
    next_chunk_id: ChunkId | None = None

    @model_validator(mode="after")
    def validate_values(self) -> ChunkContext:
        if self.title is not None and not self.title.strip():
            raise ValueError("context title must be non-empty when provided")
        if self.parent_title is not None and not self.parent_title.strip():
            raise ValueError("context parent_title must be non-empty when provided")
        if any(not part.strip() for part in self.structural_path):
            raise ValueError("context structural_path parts must be non-empty")
        return self
