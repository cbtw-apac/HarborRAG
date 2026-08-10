from __future__ import annotations

import abc
from collections.abc import Mapping

from harborrag_core.domain import (
    DocumentBlockKind,
    DocumentRelation,
    TableArtifact,
)

from .block_models import BlockContext, BlockDraft, BlockPresentation
from .macros import normalize_macro_key
from .nodes import ConfluenceNode
from .tables import TableArtifactBuilder

_INCLUDE_MACROS = frozenset({"include", "excerpt-include"})


class ConfluenceIncludeTableHandlers:
    """Handle include references and their common one-cell layout wrapper."""

    _space_key: str
    _tables: TableArtifactBuilder
    _table_artifacts: list[TableArtifact]
    _relations: list[DocumentRelation]
    _table_ordinal: int

    @abc.abstractmethod
    def _append_macro(
        self,
        parent: BlockDraft,
        node: ConfluenceNode,
        context: BlockContext,
    ) -> None: ...

    @abc.abstractmethod
    def _draft(
        self,
        node: ConfluenceNode,
        kind: DocumentBlockKind,
        parent: BlockDraft,
        context: BlockContext,
        presentation: BlockPresentation,
    ) -> BlockDraft: ...

    def _append_table(
        self,
        parent: BlockDraft,
        node: ConfluenceNode,
        context: BlockContext,
    ) -> None:
        include = self._macro_only_layout_include(node)
        if include is not None:
            self._append_macro(parent, include, context)
            return
        artifacts = self._tables.build(
            node,
            ordinal=self._table_ordinal,
            section_path=context.section_path,
            tab_path=context.tab_path,
        )
        self._table_ordinal += 1
        self._table_artifacts.extend(artifacts)
        artifact = artifacts[0]
        parent.children.append(
            self._draft(
                node,
                DocumentBlockKind.TABLE_REFERENCE,
                parent,
                context,
                BlockPresentation(
                    title=artifact.caption,
                    attributes={
                        "table_id": artifact.table_id,
                        "table_version_id": artifact.table_version_id,
                        "row_count": artifact.row_count,
                        "column_count": artifact.column_count,
                    },
                ),
            )
        )

    def _include_target_attributes(
        self,
        node: ConfluenceNode,
        macro_key: str,
    ) -> dict[str, object]:
        if normalize_macro_key(macro_key) not in _INCLUDE_MACROS:
            return {}
        return {
            "reference_kind": "include",
            "target_page_id": str(node.attributes.get("include_target_page_id") or ""),
            "target_title": str(node.attributes.get("include_target_title") or ""),
            "target_space_key": str(
                node.attributes.get("include_target_space_key") or self._space_key
            ),
        }

    def _append_include_relation(
        self,
        node: ConfluenceNode,
        macro_key: str,
        attributes: Mapping[str, object],
    ) -> None:
        if normalize_macro_key(macro_key) not in _INCLUDE_MACROS:
            return
        page_id = str(attributes.get("target_page_id") or "")
        if not page_id:
            return
        space_key = str(attributes.get("target_space_key") or self._space_key)
        self._relations.append(
            DocumentRelation(
                predicate="includes",
                target_id=f"confluence://{space_key}/{page_id}",
                target_type="document",
                metadata={
                    "source_block_id": node.source_id,
                    **(
                        {"target_title": str(attributes["target_title"])}
                        if attributes.get("target_title")
                        else {}
                    ),
                },
            )
        )

    @classmethod
    def _macro_only_layout_include(cls, table: ConfluenceNode) -> ConfluenceNode | None:
        rows = cls._descendants_before_nested_table(table, "table_row")
        if len(rows) != 1:
            return None
        cells = cls._descendants_before_nested_table(rows[0], "table_cell")
        cells += cls._descendants_before_nested_table(rows[0], "table_header")
        if len(cells) != 1:
            return None
        cell = cells[0]
        macros = cls._descendants_before_nested_table(cell, "macro")
        if len(macros) != 1:
            return None
        macro = macros[0]
        macro_key = str(macro.attributes.get("macro_name") or "")
        if normalize_macro_key(macro_key) not in _INCLUDE_MACROS:
            return None
        if cell.visible_text(exclude_kinds=frozenset({"macro", "table"})).strip():
            return None
        return macro

    @classmethod
    def _descendants_before_nested_table(
        cls,
        node: ConfluenceNode,
        kind: str,
    ) -> tuple[ConfluenceNode, ...]:
        found: list[ConfluenceNode] = []
        for child in node.children:
            if child.kind == kind:
                found.append(child)
                continue
            if child.kind != "table":
                found.extend(cls._descendants_before_nested_table(child, kind))
        return tuple(found)
