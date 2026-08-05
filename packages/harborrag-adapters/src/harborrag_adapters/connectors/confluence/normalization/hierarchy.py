from __future__ import annotations

from harborrag_core.chunking import CanonicalIdentityBuilder, encoded_identifier
from harborrag_core.domain import (
    DocumentBlock,
    DocumentBlockKind,
    DocumentElement,
    DocumentRelation,
    TableArtifact,
)

from .block_handlers import ConfluenceBlockHandlers
from .block_models import (
    BlockContext,
    BlockDraft,
    BlockPresentation,
    CanonicalBuildResult,
    ConfluenceBuildContext,
)
from .macros import ConfluenceMacroHandlerRegistry
from .nodes import ConfluenceNode
from .table_evidence import build_table_evidence_elements
from .tables import TableArtifactBuilder


class ConfluenceHierarchyBuilder(ConfluenceBlockHandlers):
    """Build deterministic sections and nested containers from source nodes."""

    def __init__(
        self,
        build_context: ConfluenceBuildContext,
        macro_registry: ConfluenceMacroHandlerRegistry | None = None,
    ) -> None:
        self._document_id = build_context.document_id
        self._source_url = build_context.source_url
        self._title = build_context.title
        self._identity = CanonicalIdentityBuilder()
        self._tables = TableArtifactBuilder(
            document_id=build_context.document_id,
            document_version_id=build_context.document_version_id,
            source_version=build_context.source_version,
            source_url=build_context.source_url,
            identity=self._identity,
        )
        self._macros = macro_registry or ConfluenceMacroHandlerRegistry()
        self._table_artifacts: list[TableArtifact] = []
        self._warnings: list[str] = []
        self._relations: list[DocumentRelation] = []
        self._table_ordinal = 0
        self._section_occurrences: dict[tuple[str, ...], int] = {}

    def build(self, root_node: ConfluenceNode) -> CanonicalBuildResult:
        root = BlockDraft(
            block_id=encoded_identifier(
                "block",
                {"document_id": self._document_id, "root": True},
            ),
            kind=DocumentBlockKind.DOCUMENT,
            ordinal=0,
            parent_block_id=None,
            source_block_id=root_node.source_id,
            title=self._title,
        )
        root_nodes = root_node.children
        if root_node.text.strip():
            root_nodes = (
                ConfluenceNode(
                    kind="paragraph",
                    source_id=f"{root_node.source_id}:text",
                    text=root_node.text,
                ),
                *root_nodes,
            )
        self._populate(root, root_nodes, BlockContext())
        block = root.model(self._source_url)
        elements = build_table_evidence_elements(
            tuple(self._flatten_elements(block)),
            tuple(self._table_artifacts),
        )
        return CanonicalBuildResult(
            blocks=(block,),
            elements=elements,
            tables=tuple(self._table_artifacts),
            relations=tuple(self._relations),
            warnings=tuple(dict.fromkeys(self._warnings)),
        )

    def _populate(
        self,
        parent: BlockDraft,
        nodes: tuple[ConfluenceNode, ...],
        base_context: BlockContext,
    ) -> None:
        section_stack: list[tuple[int, BlockDraft, BlockContext]] = []
        for node in nodes:
            if node.kind == "heading":
                level = self._heading_level(node)
                while section_stack and section_stack[-1][0] >= level:
                    section_stack.pop()
                enclosing_context = section_stack[-1][2] if section_stack else base_context
                enclosing_parent = section_stack[-1][1] if section_stack else parent
                section, context = self._section(
                    node,
                    level,
                    enclosing_context,
                    enclosing_parent,
                )
                enclosing_parent.children.append(section)
                section_stack.append((level, section, context))
                continue
            target, context = (
                (section_stack[-1][1], section_stack[-1][2])
                if section_stack
                else (parent, base_context)
            )
            self._append_node(target, node, context)

    def _section(
        self,
        node: ConfluenceNode,
        level: int,
        context: BlockContext,
        parent: BlockDraft,
    ) -> tuple[BlockDraft, BlockContext]:
        title = node.visible_text() or "Untitled section"
        # Two headings can share identical text at the same nesting depth (a
        # repeated heading, or headings inside sibling panels/expands that
        # don't otherwise affect the identity path). Without disambiguation
        # they hash to the same section_id, which either violates the
        # sibling-uniqueness invariant on DocumentBlock or silently aliases
        # unrelated sections downstream. Only the repeat gets a suffix, so the
        # common (non-duplicated) case keeps its original identity.
        candidate_path = (*context.tab_path, *context.section_path, title)
        occurrence = self._section_occurrences.get(candidate_path, 0)
        self._section_occurrences[candidate_path] = occurrence + 1
        path_title = title if occurrence == 0 else f"{title} ({occurrence + 1})"
        section_path = (*context.section_path, path_title)
        # Tab titles participate in identity so equal headings in sibling tabs
        # remain independent without changing the human-readable section path.
        section_id = self._identity.section_id(
            document_id=self._document_id,
            section_path=(*context.tab_path, *section_path),
        )
        section = BlockDraft(
            block_id=section_id,
            kind=DocumentBlockKind.SECTION,
            ordinal=len(parent.children),
            parent_block_id=parent.block_id,
            source_block_id=node.source_id,
            title=title,
            section_id=section_id,
            parent_section_id=context.section_id,
            section_path=section_path,
            tab_path=context.tab_path,
            container_path=context.container_path,
            attributes=self._context_attributes(context),
        )
        section_context = BlockContext(
            section_path=section_path,
            section_id=section_id,
            parent_section_id=context.section_id,
            tab_path=context.tab_path,
            container_path=context.container_path,
            container_ids=context.container_ids,
            tab_set_id=context.tab_set_id,
            tab_id=context.tab_id,
        )
        section.children.append(
            self._draft(
                node,
                DocumentBlockKind.HEADING,
                section,
                section_context,
                BlockPresentation(text=title, heading_level=level),
            )
        )
        return section, section_context

    def _draft(
        self,
        node: ConfluenceNode,
        kind: DocumentBlockKind,
        parent: BlockDraft,
        context: BlockContext,
        presentation: BlockPresentation,
    ) -> BlockDraft:
        block_id = encoded_identifier(
            "block",
            {
                "document_id": self._document_id,
                "source_block_id": node.source_id,
                "kind": kind.value,
                "parent_block_id": parent.block_id,
                "ordinal": len(parent.children),
            },
        )
        return BlockDraft(
            block_id=block_id,
            kind=kind,
            ordinal=len(parent.children),
            parent_block_id=parent.block_id,
            source_block_id=node.source_id,
            text=presentation.text,
            title=presentation.title,
            heading_level=presentation.heading_level,
            section_id=context.section_id,
            parent_section_id=context.parent_section_id,
            section_path=context.section_path,
            tab_path=context.tab_path,
            container_path=context.container_path,
            attributes={
                **self._context_attributes(context),
                **presentation.attributes,
            },
        )

    def _container_context(
        self,
        context: BlockContext,
        draft: BlockDraft,
    ) -> BlockContext:
        title = draft.title or draft.text or draft.kind.value.replace("_", " ").title()
        tab_path = context.tab_path
        tab_set_id = context.tab_set_id
        tab_id = context.tab_id
        if draft.kind == DocumentBlockKind.TAB_SET:
            tab_set_id = draft.block_id
        elif draft.kind == DocumentBlockKind.TAB:
            tab_id = draft.block_id
            tab_path = (*tab_path, title)
        contextual_kinds = {
            DocumentBlockKind.PANEL,
            DocumentBlockKind.EXPAND,
            DocumentBlockKind.TAB_SET,
            DocumentBlockKind.TAB,
            DocumentBlockKind.MACRO,
            DocumentBlockKind.UNSUPPORTED,
        }
        container_path = (
            (*context.container_path, title)
            if draft.kind in contextual_kinds
            else context.container_path
        )
        return BlockContext(
            section_path=context.section_path,
            section_id=context.section_id,
            parent_section_id=context.parent_section_id,
            tab_path=tab_path,
            container_path=container_path,
            container_ids=(*context.container_ids, draft.block_id),
            tab_set_id=tab_set_id,
            tab_id=tab_id,
        )

    @staticmethod
    def _context_attributes(context: BlockContext) -> dict[str, object]:
        return {
            "container_ids": context.container_ids,
            **({"tab_set_id": context.tab_set_id} if context.tab_set_id else {}),
            **({"tab_id": context.tab_id} if context.tab_id else {}),
        }

    @staticmethod
    def _heading_level(node: ConfluenceNode) -> int:
        value = node.attributes.get("level", 1)
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return 1
        try:
            return min(max(int(value), 1), 6)
        except ValueError:
            return 1

    @staticmethod
    def _flatten_elements(block: DocumentBlock) -> list[DocumentElement]:
        elements: list[DocumentElement] = []
        element_type = {
            DocumentBlockKind.HEADING: "heading",
            DocumentBlockKind.PARAGRAPH: "paragraph",
            DocumentBlockKind.LIST: "list",
            DocumentBlockKind.LIST_ITEM: "list_item",
            DocumentBlockKind.CODE_BLOCK: "code",
            DocumentBlockKind.QUOTE: "paragraph",
            DocumentBlockKind.TABLE_REFERENCE: "table",
            DocumentBlockKind.MEDIA_REFERENCE: "image",
        }.get(block.kind)
        if element_type is not None:
            content = block.text
            if block.kind == DocumentBlockKind.TABLE_REFERENCE:
                content = block.title or "Structured table reference"
            if content:
                metadata = {
                    "section_id": block.section_id,
                    "section_path": block.section_path,
                    "tab_path": block.tab_path,
                    "source_block_id": block.source_block_id,
                    **({"level": block.heading_level} if block.heading_level is not None else {}),
                    **dict(block.attributes),
                }
                elements.append(
                    DocumentElement(
                        id=block.block_id,
                        type=element_type,  # type: ignore[arg-type]
                        content=content,
                        metadata=metadata,
                    )
                )
        for child in block.children:
            elements.extend(ConfluenceHierarchyBuilder._flatten_elements(child))
        return elements
