"""Canonical chunk schema shared by chunking, projections and retrieval.

Stored chunks and graphs keep these enum values, so members are only ever
appended, never removed or reordered.

The ``*_PROPERTIES`` tuples list the node and relation properties of the
versioned semantic-v3 and enterprise-v1 graph contract that describe_graph
documents. No model enforces those shapes yet, so they are declared here.
"""

from __future__ import annotations

from enum import StrEnum
from re import fullmatch
from typing import Self

from pydantic import Field, field_validator, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.schemas.ids import ChunkId


class _ExtensibleIdentifier(StrEnum):
    """String enum whose known values are documented but not a closed set."""

    @classmethod
    def _missing_(cls, value: object) -> Self | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().casefold()
        if fullmatch(r"[a-z][a-z0-9_-]{0,63}", normalized) is None:
            return None
        member = str.__new__(cls, normalized)
        member._name_ = f"CUSTOM_{normalized.upper().replace('-', '_')}"
        member._value_ = normalized
        return member


class ConnectorType(_ExtensibleIdentifier):
    """Connector identifier with constants for built-in providers."""

    CONFLUENCE = "confluence"
    JIRA = "jira"
    LOCAL = "local"
    SHAREPOINT = "sharepoint"


class DocumentKind(_ExtensibleIdentifier):
    """Document classification with constants for built-in source shapes."""

    CONFLUENCE_PAGE = "confluence_page"
    JIRA_ISSUE = "jira_issue"
    ATTACHMENT = "attachment"
    LOCAL_FILE = "local_file"


class ChunkKind(StrEnum):
    """Content kind of a chunk, derived from its strategy role."""

    TEXT = "text"
    TABLE = "table"
    CODE = "code"
    COMMENT = "comment"
    EVENT = "event"
    JIRA_FIELD = "jira_field"


class RecordKind(StrEnum):
    """Whether a chunk is evidence or a route record.

    Only evidence is projected into the vector evidence collection.
    """

    ROUTE = "route"
    EVIDENCE = "evidence"


class ContainerKind(StrEnum):
    """Kind of structural container around a chunk, such as a section or a tab."""

    SECTION = "section"
    TAB_SET = "tab_set"
    TAB = "tab"
    EXPAND = "expand"
    PANEL = "panel"
    MACRO = "macro"


class RelationType(StrEnum):
    """Relation predicates, split by whether the graph projection emits them.

    Members are never removed. A relation_type already written into a graph must
    stay decodable, and GraphEntityType is an open set too.

    ``PROJECTED_RELATION_TYPES`` lists the predicates the projection emits, and
    the graph tools offer only those as filters. A filter on a predicate that is
    never emitted would return an empty result that looks like a genuine miss.

    SUPPORTS is no longer projected. 2.0.0a1 wrote (:Chunk)-[:SUPPORTS]->(:Structure),
    and the projection now writes (:Structure)-[:HAS_CHUNK]->(:Chunk) instead. So
    HAS_CHUNK is projected although it is declared among the reserved members.

    INCLUDES is Confluence transclusion, where an ``include`` macro renders another
    page's body inside this one. It is not LINKS_TO. A link is a reference the
    reader may follow, and a transclusion is content already on the page.

    The members from HAS_SECTION on, except HAS_CHUNK, are reserved. They are
    accepted on input but never projected. CHILD_OF and ATTACHED_TO are stored as
    reversed PARENT_OF and HAS_ATTACHMENT edges, and the rest describe structure
    that CONTAINS already carries.
    """

    HAS_DATA_SOURCE = "has_data_source"
    CONTAINS = "contains"
    HAS_VERSION = "has_version"
    SUPPORTS = "supports"
    PARENT_OF = "parent_of"
    LINKS_TO = "links_to"
    HAS_ATTACHMENT = "has_attachment"
    REPLY_TO = "reply_to"
    BLOCKS = "blocks"
    DUPLICATES = "duplicates"
    RELATES_TO = "relates_to"
    POINTS_TO = "points_to"
    RESOLVED_AT = "resolved_at"
    INCLUDES = "includes"
    HAS_SECTION = "has_section"
    HAS_CHUNK = "has_chunk"
    HAS_TABLE = "has_table"
    HAS_COMMENT = "has_comment"
    CHILD_OF = "child_of"
    EMBEDS = "embeds"
    ATTACHED_TO = "attached_to"
    COMMENT_ON = "comment_on"
    MENTIONS = "mentions"


PROJECTED_RELATION_TYPES: tuple[RelationType, ...] = (
    RelationType.HAS_DATA_SOURCE,
    RelationType.CONTAINS,
    RelationType.HAS_VERSION,
    RelationType.HAS_CHUNK,
    RelationType.PARENT_OF,
    RelationType.LINKS_TO,
    RelationType.HAS_ATTACHMENT,
    RelationType.REPLY_TO,
    RelationType.BLOCKS,
    RelationType.DUPLICATES,
    RelationType.RELATES_TO,
    RelationType.POINTS_TO,
    RelationType.RESOLVED_AT,
    RelationType.INCLUDES,
)


COMMON_NODE_PROPERTIES: tuple[str, ...] = ("node_key", "name", "description", "entity_type")
DOCUMENT_OWNED_PROPERTIES: tuple[str, ...] = (
    "document_id",
    "document_version_id",
    "source_scope_id",
)
CHUNK_PROPERTIES: tuple[str, ...] = ("chunk_id", "section_path")
ENTITY_PROPERTIES: tuple[str, ...] = (
    "id",
    "name",
    "type",
    "description",
    "aliases",
    "support_count",
)
RELATES_PROPERTIES: tuple[str, ...] = (
    "types",
    "description",
    "weight",
    "polarities",
    "modalities",
    "support_count",
)


class ChunkContainer(StrictModel):
    """One ordered structural container surrounding a chunk."""

    container_id: str = Field(min_length=1)
    kind: ContainerKind
    ordinal: int = Field(ge=0)
    title: str | None = None

    @field_validator("container_id")
    @classmethod
    def validate_container_id(cls, value: str) -> str:
        """Reject a blank container ID."""
        if not value.strip():
            raise ValueError("container_id must be non-empty")
        return value

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        """Reject a title that is present but blank."""
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
        """Reject blank IDs and an inconsistent ancestry, parent or neighbour chain."""
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
        self._validate_containers()
        return self

    def _validate_containers(self) -> None:
        container_ids = tuple(container.container_id for container in self.containers)
        if len(set(container_ids)) != len(container_ids):
            raise ValueError("chunk hierarchy container IDs must be unique")
        container_ordinals = tuple(container.ordinal for container in self.containers)
        if len(set(container_ordinals)) != len(container_ordinals):
            raise ValueError("chunk hierarchy container ordinals must be unique")
        if container_ordinals != tuple(sorted(container_ordinals)):
            raise ValueError("chunk hierarchy containers must be ordered by ordinal")


class ChunkRelation(StrictModel):
    """A typed relation from a chunk to another stable source entity."""

    relation_type: RelationType
    target_id: str = Field(min_length=1)
    target_version_id: str | None = None

    @field_validator("target_id", "target_version_id")
    @classmethod
    def validate_target(cls, value: str | None) -> str | None:
        """Reject a blank target ID or target version ID."""
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
        """Reject blank or duplicate quality issues."""
        if any(not value.strip() for value in values):
            raise ValueError("chunk quality issues must be non-empty")
        if len(set(values)) != len(values):
            raise ValueError("chunk quality issues must not contain duplicates")
        return values
