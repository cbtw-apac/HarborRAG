from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .errors import ConfluenceNormalizationError
from .nodes import ConfluenceNode
from .values import mapping as _mapping
from .values import mapping_sequence as _mapping_sequence

_KIND_MAP = {
    "doc": "document",
    "paragraph": "paragraph",
    "heading": "heading",
    "bulletList": "list",
    "orderedList": "list",
    "listItem": "list_item",
    "codeBlock": "code_block",
    "blockquote": "quote",
    "panel": "panel",
    "expand": "expand",
    "nestedExpand": "expand",
    "table": "table",
    "tableRow": "table_row",
    "tableHeader": "table_header",
    "tableCell": "table_cell",
    "mediaSingle": "group",
    "mediaGroup": "group",
    "media": "media",
    "inlineCard": "link",
    "blockCard": "link",
    "extension": "macro",
    "bodiedExtension": "macro",
    "inlineExtension": "macro",
    "hardBreak": "text",
    "rule": "unsupported",
}


class _AdfFrame:
    """One pending node in the iterative post-order ADF build."""

    __slots__ = ("value", "path", "children_raw", "next_index", "built")

    def __init__(self, value: Mapping[str, Any], path: tuple[int, ...]) -> None:
        self.value = value
        self.path = path
        self.children_raw = tuple(_mapping_sequence(value.get("content")))
        self.next_index = 0
        self.built: list[ConfluenceNode] = []


class AdfDocumentParser:
    """Convert Atlassian Document Format into the shared structural node tree."""

    def parse(self, value: Mapping[str, Any] | str) -> ConfluenceNode:
        try:
            document = json.loads(value) if isinstance(value, str) else dict(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ConfluenceNormalizationError("Confluence ADF body is not valid JSON") from exc
        if not isinstance(document, Mapping) or document.get("type") != "doc":
            raise ConfluenceNormalizationError("Confluence ADF root must have type 'doc'")
        return self._node(document, path=(0,))

    def _node(self, value: Mapping[str, Any], *, path: tuple[int, ...]) -> ConfluenceNode:
        """Build the node tree post-order with an explicit stack.

        A deeply nested ADF document (panels/expands/lists nested dozens of
        levels deep) must not grow the Python call stack, mirroring the
        explicit-stack traversal already used for page hierarchies in
        content.py.
        """
        root = _AdfFrame(value, path)
        stack: list[_AdfFrame] = [root]
        while True:
            frame = stack[-1]
            if frame.next_index < len(frame.children_raw):
                child_value = frame.children_raw[frame.next_index]
                child_path = (*frame.path, frame.next_index)
                frame.next_index += 1
                stack.append(_AdfFrame(child_value, child_path))
                continue
            node = self._build_node(frame.value, path=frame.path, children=tuple(frame.built))
            stack.pop()
            if not stack:
                return node
            stack[-1].built.append(node)

    def _build_node(
        self,
        value: Mapping[str, Any],
        *,
        path: tuple[int, ...],
        children: tuple[ConfluenceNode, ...],
    ) -> ConfluenceNode:
        source_type = str(value.get("type") or "unsupported")
        attributes = dict(_mapping(value.get("attrs")))
        source_id = str(
            attributes.get("localId")
            or attributes.get("id")
            or f"adf:{'.'.join(str(part) for part in path)}"
        )
        if source_type == "text":
            return self._text_node(value, source_id)
        kind = _KIND_MAP.get(source_type, "unsupported")
        if source_type == "hardBreak":
            return ConfluenceNode(kind="text", source_id=source_id, text="\n")
        if source_type in {"bulletList", "orderedList"}:
            attributes["ordered"] = source_type == "orderedList"
        if kind == "heading":
            attributes["level"] = _integer(attributes.get("level"), 1)
        if kind == "macro":
            attributes["macro_name"] = str(
                attributes.get("extensionKey") or attributes.get("extensionType") or source_type
            )
            attributes["parameters"] = _mapping(attributes.get("parameters"))
        if kind == "link":
            attributes["href"] = str(attributes.get("url") or "")
        return ConfluenceNode(
            kind=kind,
            source_id=source_id,
            attributes=attributes,
            children=children,
        )

    @staticmethod
    def _text_node(value: Mapping[str, Any], source_id: str) -> ConfluenceNode:
        text = str(value.get("text") or "")
        marks = _mapping_sequence(value.get("marks"))
        link = next(
            (_mapping(mark.get("attrs")) for mark in marks if mark.get("type") == "link"),
            None,
        )
        if link is None:
            return ConfluenceNode(kind="text", source_id=source_id, text=text)
        return ConfluenceNode(
            kind="link",
            source_id=source_id,
            text=text,
            attributes={"href": str(link.get("href") or "")},
        )


def _integer(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (str, int, float)):
        try:
            return int(value)
        except ValueError:
            return default
    return default
