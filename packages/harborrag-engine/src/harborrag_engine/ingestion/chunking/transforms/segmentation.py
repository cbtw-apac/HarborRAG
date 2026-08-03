from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from harborrag_core.contracts.chunking import SourceSpan, SplitBoundaryKind, TokenCounter
from harborrag_core.domain.document import Document
from harborrag_core.domain.element import DocumentElement

from ..config import ChunkingProfile
from ..schemas import ChunkUnit


def integer_metadata(metadata: Mapping[str, Any], *keys: str) -> int | None:
    """Return the first integer metadata value, excluding booleans."""

    for key in keys:
        value = metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def element_span(element_id: str, content: str, metadata: Mapping[str, Any]) -> SourceSpan:
    """Build the best source span available for a normalized element."""

    start_offset = integer_metadata(metadata, "start_offset")
    end_offset = integer_metadata(metadata, "end_offset")
    if start_offset is None or end_offset is None:
        start_offset, end_offset = 0, len(content)

    start_line = integer_metadata(metadata, "start_line", "line_start", "line")
    end_line = integer_metadata(metadata, "end_line", "line_end")
    if start_line is not None and end_line is None:
        end_line = start_line + content.count("\n")
    if end_line is not None and start_line is None:
        start_line = end_line - content.count("\n")

    page_start = integer_metadata(metadata, "page_start", "page")
    page_end = integer_metadata(metadata, "page_end")
    if page_start is not None and page_end is None:
        page_end = page_start
    if page_end is not None and page_start is None:
        page_start = page_end

    return SourceSpan(
        start_offset=start_offset,
        end_offset=end_offset,
        start_line=start_line,
        end_line=end_line,
        page_start=page_start,
        page_end=page_end,
        element_ids=(element_id,),
    )


class DocumentStructureSegmenter:
    """Convert canonical elements into provenance-rich structural units."""

    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    def segment(
        self,
        document: Document,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Convert normalized document elements into source-ordered units."""

        headings: list[str] = []
        heading_ids: list[str] = []
        units: list[ChunkUnit] = []
        heading_level_offset = 0

        for element in document.content:
            content = element.content or ""
            if not content or not content.strip():
                continue

            if element.type == "heading":
                level = self._heading_level(element.metadata)
                if level == 1 and content.strip() == document.title.strip():
                    headings.clear()
                    heading_ids.clear()
                    heading_level_offset = 1
                    continue
                level = max(level - heading_level_offset, 1)
                headings = headings[: level - 1]
                heading_ids = heading_ids[: level - 1]
                headings.append(content.strip())
                heading_ids.append(element.id)
                continue

            structural_path = (
                self._structural_path(element, headings) if profile.preserve_sections else ()
            )
            boundary_kind = self._boundary_kind(document, element)
            role = self._role(element, boundary_kind)
            anchor = self._anchor(element, boundary_kind, structural_path)
            merge_group = self._merge_group(element, role, structural_path)
            token_count = self._token_counter.count(content)
            if token_count < 1:
                continue
            role_tokens = frozenset(role.casefold().replace("_", ".").split("."))
            hard_boundary = role in {"table", "code", "figure", "caption", "json"} or (
                "comment" in role_tokens
            )
            units.append(
                ChunkUnit(
                    anchor=anchor,
                    content=content,
                    token_count=token_count,
                    role=role,
                    structural_path=structural_path,
                    source_span=element_span(element.id, content, element.metadata),
                    merge_group=merge_group,
                    boundary_kind=boundary_kind,
                    hard_boundary_before=hard_boundary,
                    hard_boundary_after=hard_boundary,
                    metadata={
                        "element_type": element.type,
                        "heading_element_ids": tuple(heading_ids),
                        **element.metadata,
                    },
                )
            )
        return tuple(units)

    @staticmethod
    def _structural_path(
        element: DocumentElement,
        headings: list[str],
    ) -> tuple[str, ...]:
        supplied = element.metadata.get("section_path")
        section_path = (
            tuple(str(value).strip() for value in supplied if str(value).strip())
            if isinstance(supplied, (list, tuple))
            else tuple(headings)
        )
        tab_value = element.metadata.get("tab_path")
        tab_path = (
            tuple(str(value).strip() for value in tab_value if str(value).strip())
            if isinstance(tab_value, (list, tuple))
            else ()
        )
        if not tab_path:
            return section_path
        if not section_path:
            return tab_path
        return (section_path[0], *tab_path, *section_path[1:])

    @staticmethod
    def _heading_level(metadata: Mapping[str, Any]) -> int:
        value = metadata.get("level", 1)
        if not isinstance(value, int) or isinstance(value, bool):
            return 1
        return min(max(value, 1), 6)

    @staticmethod
    def _boundary_kind(
        document: Document,
        element: DocumentElement,
    ) -> SplitBoundaryKind:
        if element.type == "table":
            return SplitBoundaryKind.TABLE
        if element.type == "code":
            kind = element.metadata.get("symbol_kind")
            return (
                SplitBoundaryKind.CODE_SYMBOL
                if isinstance(kind, str) and kind
                else SplitBoundaryKind.CODE_BLOCK
            )
        if element.type == "metadata" and (
            document.content_type
            in {"application/json", "application/jsonl", "application/x-ndjson"}
            or "json_path" in element.metadata
            or "root_type" in element.metadata
        ):
            return SplitBoundaryKind.JSON_PATH
        if element.type in {"list", "list_item", "footnote"}:
            return SplitBoundaryKind.LINE
        return SplitBoundaryKind.PARAGRAPH

    @staticmethod
    def _role(
        element: DocumentElement,
        boundary_kind: SplitBoundaryKind,
    ) -> str:
        explicit = element.metadata.get("role")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        if boundary_kind in {SplitBoundaryKind.TABLE, SplitBoundaryKind.TABLE_ROW}:
            return "table"
        if boundary_kind in {SplitBoundaryKind.CODE_BLOCK, SplitBoundaryKind.CODE_SYMBOL}:
            return "code"
        if boundary_kind == SplitBoundaryKind.JSON_PATH:
            return "json"
        if element.type in {"image", "figure"}:
            return "figure"
        if element.type == "caption":
            return "caption"
        if element.type == "footnote":
            return "footnote"
        if element.type in {"list", "list_item"}:
            return "list"
        return "text"

    @staticmethod
    def _anchor(
        element: DocumentElement,
        boundary_kind: SplitBoundaryKind,
        structural_path: tuple[str, ...],
    ) -> str:
        metadata = element.metadata
        explicit = metadata.get("anchor") or metadata.get("section_id")
        if explicit is not None:
            return str(explicit)
        if boundary_kind in {SplitBoundaryKind.TABLE, SplitBoundaryKind.TABLE_ROW}:
            return f"table:{metadata.get('table_id', element.id)}"
        if boundary_kind in {SplitBoundaryKind.CODE_BLOCK, SplitBoundaryKind.CODE_SYMBOL}:
            return f"symbol:{metadata.get('qualified_name', element.id)}"
        if boundary_kind == SplitBoundaryKind.JSON_PATH:
            return f"json:{metadata.get('json_path', element.id)}"
        if structural_path:
            path = json.dumps(structural_path, ensure_ascii=False, separators=(",", ":"))
            return f"section:{path}/element:{element.id}"
        return f"element:{element.id}"

    @staticmethod
    def _merge_group(
        element: DocumentElement,
        role: str,
        structural_path: tuple[str, ...],
    ) -> str:
        metadata = element.metadata
        explicit = metadata.get("merge_group")
        if explicit is not None:
            return str(explicit)
        path = json.dumps(structural_path, ensure_ascii=False, separators=(",", ":"))
        if role == "table":
            return f"table:{metadata.get('table_id', element.id)}"
        if role == "code":
            return f"code:{metadata.get('qualified_name', element.id)}"
        if role == "json":
            return f"json:{metadata.get('json_path', element.id)}"
        if role in {"figure", "caption"}:
            return f"visual:{metadata.get('parent_element_id', element.id)}"
        return f"{role}:section:{path}"
