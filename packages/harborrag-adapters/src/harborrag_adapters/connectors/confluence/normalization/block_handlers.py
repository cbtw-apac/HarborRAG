"""Block, macro and reference handling, the middle layer of the block handler chain.

Handles:
- Dispatch of every non-heading node kind
- Macro blocks resolved through the handler registry
- Link and media reference blocks with their relations
- Unsupported nodes preserved as blocks

Does NOT decide sections or block ids; the hierarchy builder does through abstract hooks.
"""

from __future__ import annotations

import abc
import re
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

from harborrag_core.domain import (
    DocumentBlockKind,
    DocumentRelation,
    TableArtifact,
)

from .block_models import BlockContext, BlockDraft, BlockPresentation
from .include_handlers import ConfluenceIncludeTableHandlers
from .macros import ConfluenceMacroHandlerRegistry
from .nodes import INLINE_KINDS, ConfluenceNode
from .tables import TableArtifactBuilder

_PAGE_ID_PATTERN = re.compile(r"/pages/(\d+)(?:/|$)")
_BLOCK_KIND_BY_NODE = {
    "paragraph": DocumentBlockKind.PARAGRAPH,
    "list": DocumentBlockKind.LIST,
    "list_item": DocumentBlockKind.LIST_ITEM,
    "code_block": DocumentBlockKind.CODE_BLOCK,
    "quote": DocumentBlockKind.QUOTE,
    "panel": DocumentBlockKind.PANEL,
    "expand": DocumentBlockKind.EXPAND,
    # ADF tabs arrive here directly and must map to the same kinds as the tabs macro
    # so tab_path is built identically.
    "tab_set": DocumentBlockKind.TAB_SET,
    "tab": DocumentBlockKind.TAB,
}
_NESTED_BLOCK_KINDS = frozenset({*_BLOCK_KIND_BY_NODE, "table", "macro", "unsupported"})


class ConfluenceBlockHandlers(ConfluenceIncludeTableHandlers, abc.ABC):
    """Middle layer of the handler chain for non-heading nodes.

    Needs the builder to provide ``_populate``, ``_draft`` and ``_container_context``.
    """

    _document_id: str
    _space_key: str
    _macros: ConfluenceMacroHandlerRegistry
    _tables: TableArtifactBuilder
    _table_artifacts: list[TableArtifact]
    _warnings: list[str]
    _relations: list[DocumentRelation]
    _table_ordinal: int

    def _append_node(
        self,
        parent: BlockDraft,
        node: ConfluenceNode,
        context: BlockContext,
    ) -> None:
        """Dispatch one node to the handler for its kind.

        Macros, tables, references and unsupported nodes have their own handlers. Groups,
        documents, text and unmapped kinds are flattened. Mapped kinds become a block whose
        text excludes nested block kinds, so text is never emitted twice.
        """

        if node.kind in {"group", "document", "text"}:
            self._append_group(parent, node, context)
            return
        if node.kind == "macro":
            self._append_macro(parent, node, context)
            return
        if node.kind == "table":
            self._append_table(parent, node, context)
            return
        if node.kind in {"link", "media"}:
            self._append_reference(parent, node, context)
            return
        if node.kind == "unsupported":
            self._append_unsupported(parent, node, context)
            return
        kind = _BLOCK_KIND_BY_NODE.get(node.kind)
        if kind is None:
            self._append_group(parent, node, context)
            return
        text = node.visible_text(exclude_kinds=_NESTED_BLOCK_KINDS)
        draft = self._draft(
            node,
            kind,
            parent,
            context,
            BlockPresentation(
                text=text or None,
                title=self._container_title(node, kind),
                attributes=self._node_attributes(node),
            ),
        )
        parent.children.append(draft)
        child_context = self._container_context(context, draft)
        child_nodes = tuple(child for child in node.children if child.kind not in INLINE_KINDS)
        if child_nodes:
            # TODO: bound the nesting depth; deeply nested nodes raise RecursionError.
            self._populate(draft, child_nodes, child_context)
        self._append_inline_references(draft, node, child_context)

    def _append_group(
        self,
        parent: BlockDraft,
        node: ConfluenceNode,
        context: BlockContext,
    ) -> None:
        """Populate a node's children into ``parent``, with the node's own text first."""

        children = node.children
        if node.text.strip():
            # Own text leads the group's content.
            own_text = ConfluenceNode(kind="text", source_id=node.source_id, text=node.text)
            children = (own_text, *children)
        self._populate(parent, children, context)

    def _append_macro(
        self,
        parent: BlockDraft,
        node: ConfluenceNode,
        context: BlockContext,
    ) -> None:
        """Append a block for a macro and, for includes, an ``includes`` relation.

        The registry decides kind, title and parameters; an include target title replaces
        the title. The text is the macro body, else the include target title.
        """

        key = str(node.attributes.get("macro_name") or "unknown")
        parameters = node.attributes.get("parameters")
        handler = self._macros.resolve(key)
        handling = handler.handle(
            key,
            parameters if isinstance(parameters, Mapping) else {},
        )
        if handling.warning:
            self._warnings.append(handling.warning)
        include_target = self._include_target_attributes(node, key)
        include_title = include_target.get("target_title")
        draft = self._draft(
            node,
            handling.kind,
            parent,
            context,
            BlockPresentation(
                text=(
                    node.visible_text(
                        exclude_kinds=_NESTED_BLOCK_KINDS,
                        verbatim=handling.kind == DocumentBlockKind.CODE_BLOCK,
                    )
                    or (str(include_title) if include_title else None)
                ),
                title=(str(include_title) if include_title else handling.title),
                attributes={
                    "macro_key": key,
                    "macro_id": str(node.attributes.get("macroId") or node.source_id),
                    "parameters": dict(handling.parameters),
                    "emits_container": handler.emits_container,
                    "emits_visible_content": handler.emits_visible_content,
                    "emits_table": handler.emits_table,
                    "needs_rendered_fallback": handler.needs_rendered_fallback,
                    **include_target,
                },
            ),
        )
        parent.children.append(draft)
        self._append_include_relation(node, key, include_target)
        child_context = self._container_context(context, draft)
        child_nodes = tuple(child for child in node.children if child.kind not in INLINE_KINDS)
        if child_nodes:
            self._populate(draft, child_nodes, child_context)
        self._append_inline_references(draft, node, child_context)

    def _append_reference(
        self,
        parent: BlockDraft,
        node: ConfluenceNode,
        context: BlockContext,
    ) -> None:
        """Append a link or media reference block and a relation when a target is known.

        A link with a page id yields ``links_to``; media with an attachment id yields
        ``has_attachment``.
        """

        kind = (
            DocumentBlockKind.LINK_REFERENCE
            if node.kind == "link"
            else DocumentBlockKind.MEDIA_REFERENCE
        )
        attributes = self._reference_attributes(node)
        parent.children.append(
            self._draft(
                node,
                kind,
                parent,
                context,
                BlockPresentation(
                    text=node.visible_text() or None,
                    attributes=attributes,
                ),
            )
        )
        target = attributes.get("target_page_id") or attributes.get("attachment_id")
        if isinstance(target, str) and target:
            self._relations.append(
                DocumentRelation(
                    predicate="links_to" if node.kind == "link" else "has_attachment",
                    target_id=target,
                    target_type="document" if node.kind == "link" else "attachment",
                    metadata={"source_block_id": node.source_id},
                )
            )

    def _append_unsupported(
        self,
        parent: BlockDraft,
        node: ConfluenceNode,
        context: BlockContext,
    ) -> None:
        """Preserve an unsupported node as a block and record a warning."""

        # TODO: report the source node type instead of the generic ``unsupported`` kind.
        self._warnings.append(f"unsupported Confluence block preserved: {node.kind}")
        draft = self._draft(
            node,
            DocumentBlockKind.UNSUPPORTED,
            parent,
            context,
            BlockPresentation(
                text=node.visible_text(exclude_kinds=_NESTED_BLOCK_KINDS) or None,
                attributes={"source_kind": node.kind},
            ),
        )
        parent.children.append(draft)
        child_context = self._container_context(context, draft)
        child_nodes = tuple(child for child in node.children if child.kind not in INLINE_KINDS)
        if child_nodes:
            self._populate(draft, child_nodes, child_context)
        self._append_inline_references(draft, node, child_context)

    def _append_inline_references(
        self,
        parent: BlockDraft,
        node: ConfluenceNode,
        context: BlockContext,
    ) -> None:
        """Create reference blocks for the links and media in a node's inline content.

        Block children are skipped; ``_append_node`` adds their references.
        """

        for child in node.children:
            if child.kind in {"link", "media"}:
                self._append_reference(parent, child, context)
            elif child.kind in INLINE_KINDS:
                self._append_inline_references(parent, child, context)

    @staticmethod
    def _reference_attributes(node: ConfluenceNode) -> dict[str, object]:
        """Return reference block attributes; the page id falls back to the ``/pages/<id>`` URL."""

        if node.kind == "media":
            return {
                "attachment_id": str(
                    node.attributes.get("attachment_id") or node.attributes.get("id") or ""
                ),
                "filename": str(
                    node.attributes.get("filename") or node.attributes.get("alt") or ""
                ),
                "media_type": str(node.attributes.get("type") or ""),
            }
        href = str(node.attributes.get("href") or "")
        safe_url = _safe_url(href)
        match = _PAGE_ID_PATTERN.search(urlsplit(safe_url).path)
        return {
            "anchor_text": node.visible_text(),
            "target_url": safe_url,
            "target_page_id": str(
                node.attributes.get("target_page_id") or (match.group(1) if match else "")
            ),
        }

    @staticmethod
    def _container_title(
        node: ConfluenceNode,
        kind: DocumentBlockKind,
    ) -> str | None:
        """Return the ``title`` attribute, or ``Details`` for an untitled expand."""

        value = node.attributes.get("title")
        if value and str(value).strip():
            return str(value).strip()
        if kind == DocumentBlockKind.EXPAND:
            return "Details"
        return None

    @staticmethod
    def _node_attributes(node: ConfluenceNode) -> dict[str, object]:
        """Keep only the allow-listed scalar attributes of a node."""

        allowed = {"ordered", "panelType", "panel-type", "type", "icon"}
        return {
            str(key): value
            for key, value in node.attributes.items()
            if key in allowed and isinstance(value, (str, int, bool))
        }

    @abc.abstractmethod
    def _populate(
        self,
        parent: BlockDraft,
        nodes: tuple[ConfluenceNode, ...],
        base_context: BlockContext,
    ) -> None:
        """Append ``nodes`` under ``parent`` with section rules; the builder provides it."""

        ...

    @abc.abstractmethod
    def _draft(
        self,
        node: ConfluenceNode,
        kind: DocumentBlockKind,
        parent: BlockDraft,
        context: BlockContext,
        presentation: BlockPresentation,
    ) -> BlockDraft:
        """Create the draft for ``node`` as next child of ``parent``; the builder provides it."""

        ...

    @abc.abstractmethod
    def _container_context(
        self,
        context: BlockContext,
        draft: BlockDraft,
    ) -> BlockContext:
        """Return the context for a container ``draft``'s children; the builder provides it."""

        ...


def _safe_url(value: str) -> str:
    """Return an http(s) or relative URL without its query, else an empty string.

    Other schemes such as ``javascript:`` and ``data:`` are rejected.
    """

    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", parsed.fragment))
