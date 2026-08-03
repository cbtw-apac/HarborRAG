from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

from .errors import ConfluenceNormalizationError
from .nodes import ConfluenceNode

_TAG_KINDS = {
    "p": "paragraph",
    "ul": "list",
    "ol": "list",
    "li": "list_item",
    "pre": "code_block",
    "blockquote": "quote",
    "table": "table",
    "tr": "table_row",
    "td": "table_cell",
    "th": "table_header",
    "caption": "caption",
    "a": "link",
    "img": "media",
    "ri:attachment": "media",
    "ri:page": "link",
    "ac:structured-macro": "macro",
    "ac:rich-text-body": "group",
    "ac:plain-text-body": "group",
}
_VOID_TAGS = frozenset({"br", "hr", "img", "meta", "link", "input", "ri:attachment", "ri:page"})


@dataclass(slots=True)
class _MutableNode:
    tag: str
    attributes: dict[str, str]
    path: tuple[int, ...]
    text: list[str] = field(default_factory=list)
    children: list[_MutableNode] = field(default_factory=list)


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _MutableNode("document", {}, (0,))
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        parent = self._stack[-1]
        node = _MutableNode(
            tag.lower(),
            {key.lower(): value or "" for key, value in attrs},
            (*parent.path, len(parent.children)),
        )
        parent.children.append(node)
        if tag.lower() not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].text.append(data)


class ConfluenceMarkupParser:
    """Parse storage-format XML or rendered HTML with one tolerant tree policy."""

    def parse(self, value: str) -> ConfluenceNode:
        parser = _TreeParser()
        try:
            parser.feed(value)
            parser.close()
        except (AssertionError, ValueError) as exc:
            raise ConfluenceNormalizationError("Confluence markup body is malformed") from exc
        if not parser.root.children and not "".join(parser.root.text).strip():
            raise ConfluenceNormalizationError("Confluence markup body is empty")
        return self._node(parser.root)

    def _node(self, value: _MutableNode) -> ConfluenceNode:
        tag = value.tag
        attributes: dict[str, object] = dict(value.attributes)
        source_id = (
            value.attributes.get("data-local-id")
            or value.attributes.get("data-node-id")
            or value.attributes.get("id")
            or value.attributes.get("ac:macro-id")
            or f"markup:{'.'.join(str(part) for part in value.path)}"
        )
        children = tuple(self._node(child) for child in value.children)
        kind = self._kind(value)
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            attributes["level"] = int(tag[1])
        if tag in {"ul", "ol"}:
            attributes["ordered"] = tag == "ol"
        if tag == "ac:structured-macro":
            attributes["macro_name"] = value.attributes.get("ac:name", "unknown")
            attributes["parameters"] = self._macro_parameters(value)
            children = tuple(child for child in children if child.kind != "macro_parameter")
        if tag == "ri:attachment":
            attributes["filename"] = value.attributes.get("ri:filename", "")
            attributes["attachment_id"] = value.attributes.get("ri:content-id", "")
        if tag == "ri:page":
            attributes["target_page_id"] = value.attributes.get("ri:content-id", "")
            attributes["target_title"] = value.attributes.get("ri:content-title", "")
        return ConfluenceNode(
            kind=kind,
            source_id=source_id,
            text="".join(value.text),
            attributes=attributes,
            children=children,
        )

    @staticmethod
    def _kind(value: _MutableNode) -> str:
        tag = value.tag
        if tag.startswith("h") and len(tag) == 2 and tag[1] in "123456":
            return "heading"
        if tag == "ac:parameter":
            return "macro_parameter"
        if tag == "details":
            return "expand"
        classes = set(value.attributes.get("class", "").lower().split())
        if "confluence-information-macro" in classes or "panel" in classes:
            return "panel"
        return _TAG_KINDS.get(tag, "group")

    @staticmethod
    def _macro_parameters(value: _MutableNode) -> dict[str, str]:
        parameters: dict[str, str] = {}
        for child in value.children:
            if child.tag != "ac:parameter":
                continue
            name = child.attributes.get("ac:name", "")
            parameter_value = "".join(child.text).strip()
            if name and parameter_value:
                parameters[name] = parameter_value
        return parameters
