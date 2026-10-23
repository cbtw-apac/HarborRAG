"""Atlassian Document Format parser producing ``ConfluenceNode`` trees.

Handles:
- ADF node types mapped onto the shared node kinds
- Macro, link, heading and list attributes
- Inline atoms such as mentions and dates

Does NOT parse storage-format XML; the markup parser does.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
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
    "taskList": "list",
    "taskItem": "list_item",
    "decisionList": "list",
    "decisionItem": "list_item",
    "layoutSection": "group",
    "layoutColumn": "group",
    "caption": "caption",
    "codeBlock": "code_block",
    "blockquote": "quote",
    "panel": "panel",
    "expand": "expand",
    "nestedExpand": "expand",
    "tabsContainer": "tab_set",
    "tabsPage": "tab",
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
    """Pending node in the iterative post-order ADF build."""

    __slots__ = ("value", "path", "children_raw", "next_index", "built")

    def __init__(self, value: Mapping[str, Any], path: tuple[int, ...]) -> None:
        self.value = value
        self.path = path
        self.children_raw = tuple(_mapping_sequence(value.get("content")))
        self.next_index = 0
        self.built: list[ConfluenceNode] = []


class AdfDocumentParser:
    """Converter from ADF to the shared node tree.

    Stateless; each ``parse`` call builds a fresh tree.
    """

    def parse(self, value: Mapping[str, Any] | str) -> ConfluenceNode:
        """Parse an ADF document into a node tree.

        Args:
            value: ADF document as a mapping or a JSON string.

        Returns:
            Root node of the parsed tree.

        Raises:
            ConfluenceNormalizationError: If the value is not valid JSON or the root node
                is not of type ``doc``.
        """

        try:
            document = json.loads(value) if isinstance(value, str) else dict(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ConfluenceNormalizationError("Confluence ADF body is not valid JSON") from exc
        if not isinstance(document, Mapping) or document.get("type") != "doc":
            raise ConfluenceNormalizationError("Confluence ADF root must have type 'doc'")
        return self._node(document, path=(0,))

    def _node(self, value: Mapping[str, Any], *, path: tuple[int, ...]) -> ConfluenceNode:
        """Build the node tree post-order with an explicit stack."""

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
        """Build one node from its raw ADF value and its already built children.

        The source id is ``localId`` or ``id``, else ``adf:<path>``. Inline atoms and hard
        breaks become text nodes.
        """

        source_type = str(value.get("type") or "unsupported")
        attributes = dict(_mapping(value.get("attrs")))
        source_id = str(
            attributes.get("localId")
            or attributes.get("id")
            or f"adf:{'.'.join(str(part) for part in path)}"
        )
        if source_type == "text":
            return self._text_node(value, source_id)
        atom_text = _inline_atom_text(source_type, attributes)
        if atom_text is not None:
            return ConfluenceNode(kind="text", source_id=source_id, text=atom_text)
        kind = _KIND_MAP.get(source_type, "unsupported")
        if source_type == "hardBreak":
            return ConfluenceNode(kind="text", source_id=source_id, text="\n")
        if source_type in {"bulletList", "orderedList"}:
            attributes["ordered"] = source_type == "orderedList"
        if kind == "heading":
            attributes["level"] = _integer(attributes.get("level"), 1)
        # TODO: set include_target_* for ADF include macros so blocks carry the target and relation.
        if kind == "macro":
            attributes["macro_name"] = str(
                attributes.get("extensionKey") or attributes.get("extensionType") or source_type
            )
            attributes["parameters"] = _macro_parameters(_mapping(attributes.get("parameters")))
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
        """Build a text node, or a link node if the text carries a ``link`` mark."""

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


def _macro_parameters(parameters: Mapping[str, Any]) -> Mapping[str, Any]:
    """Unwrap ``macroParams.<name>.value`` into plain parameters, dropping ``macroMetadata``.

    Without ``macroParams`` the parameters are returned unchanged.
    """

    macro_params = _mapping(parameters.get("macroParams"))
    if not macro_params:
        return parameters
    plain = {
        key: value
        for key, value in parameters.items()
        if key not in {"macroParams", "macroMetadata"}
    }
    for name, entry in macro_params.items():
        value = entry.get("value") if isinstance(entry, Mapping) else entry
        if value is not None:
            plain[str(name)] = value
    return plain


def _inline_atom_text(source_type: str, attributes: Mapping[str, Any]) -> str | None:
    """Return the text of an inline atom kept in ``attrs``, or None for other nodes."""

    if source_type in {"mention", "status"}:
        return str(attributes.get("text") or "")
    if source_type == "emoji":
        return str(attributes.get("text") or attributes.get("shortName") or "")
    if source_type == "date":
        return _date_text(attributes.get("timestamp"))
    return None


def _date_text(value: object) -> str:
    """Return an epoch-millisecond timestamp as a UTC ISO date, or empty if invalid."""

    try:
        milliseconds = int(str(value))
        return datetime.fromtimestamp(milliseconds / 1000, UTC).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _integer(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (str, int, float)):
        try:
            return int(value)
        except ValueError:
            return default
    return default
