"""Intermediate block, context and result types for page hierarchy building.

Handles:
- Mutable block drafts and their frozen models
- Section, tab and container position
- Page identity and build result values
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harborrag_core.chunking import SourceLocator
from harborrag_core.domain import (
    DocumentBlock,
    DocumentBlockKind,
    DocumentElement,
    DocumentRelation,
    TableArtifact,
)


@dataclass(slots=True)
class BlockDraft:
    """Mutable block under construction, later converted to a ``DocumentBlock``.

    Attributes:
        ordinal: Index among the parent's children, set when the draft is created.
        children: Child drafts, appended while the tree is populated.
    """

    block_id: str
    kind: DocumentBlockKind
    ordinal: int
    parent_block_id: str | None
    source_block_id: str | None
    text: str | None = None
    title: str | None = None
    heading_level: int | None = None
    section_id: str | None = None
    parent_section_id: str | None = None
    section_path: tuple[str, ...] = ()
    tab_path: tuple[str, ...] = ()
    container_path: tuple[str, ...] = ()
    attributes: dict[str, object] = field(default_factory=dict)
    children: list[BlockDraft] = field(default_factory=list)

    def model(self, source_url: str) -> DocumentBlock:
        """Return the frozen ``DocumentBlock`` for this draft and its children."""

        return DocumentBlock(
            block_id=self.block_id,
            kind=self.kind,
            ordinal=self.ordinal,
            text=self.text,
            title=self.title,
            heading_level=self.heading_level,
            parent_block_id=self.parent_block_id,
            source_block_id=self.source_block_id,
            source_locator=SourceLocator(
                uri=source_url,
                source_element_ids=((self.source_block_id,) if self.source_block_id else ()),
            ),
            section_id=self.section_id,
            parent_section_id=self.parent_section_id,
            section_path=self.section_path,
            tab_path=self.tab_path,
            container_path=self.container_path,
            attributes=self.attributes,
            children=tuple(child.model(source_url) for child in self.children),
        )


@dataclass(frozen=True, slots=True)
class BlockContext:
    """Immutable position of a node: enclosing section, tab and container.

    Attributes:
        container_ids: Enclosing container block ids, outermost first.
    """

    section_path: tuple[str, ...] = ()
    section_id: str | None = None
    parent_section_id: str | None = None
    tab_path: tuple[str, ...] = ()
    container_path: tuple[str, ...] = ()
    container_ids: tuple[str, ...] = ()
    tab_set_id: str | None = None
    tab_id: str | None = None


@dataclass(frozen=True, slots=True)
class BlockPresentation:
    """Text, title, heading level and attributes a handler gives to a new draft."""

    text: str | None = None
    title: str | None = None
    heading_level: int | None = None
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConfluenceBuildContext:
    """Page identity values for deriving block ids and locators."""

    document_id: str
    document_version_id: str
    source_version: str
    source_url: str
    space_key: str
    title: str


@dataclass(frozen=True, slots=True)
class CanonicalBuildResult:
    """Blocks, elements, tables, relations and warnings built for one page."""

    blocks: tuple[DocumentBlock, ...]
    elements: tuple[DocumentElement, ...]
    tables: tuple[TableArtifact, ...]
    relations: tuple[DocumentRelation, ...]
    warnings: tuple[str, ...]
