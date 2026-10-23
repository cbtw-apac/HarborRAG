"""Storage-format and rendered HTML parser producing ``ConfluenceNode`` trees.

Handles:
- Tolerant tree building for unclosed or stray tags
- Tag-to-kind mapping, including headings and panels
- Macro parameters and include targets
"""

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
    # A macro's plain-text body (code, noformat) is the macro's own content.
    "ac:plain-text-body": "inline",
    "#text": "text",
}
# Formatting and link wrappers whose text reads as part of the enclosing block.
_INLINE_TAGS = frozenset(
    {
        "abbr",
        "b",
        "big",
        "cite",
        "code",
        "del",
        "em",
        "font",
        "i",
        "ins",
        "kbd",
        "mark",
        "q",
        "s",
        "samp",
        "small",
        "span",
        "strike",
        "strong",
        "sub",
        "sup",
        "time",
        "tt",
        "u",
        "var",
        "ac:emoticon",
        "ac:image",
        "ac:inline-comment-marker",
        "ac:link",
        "ac:link-body",
        "ac:plain-text-link-body",
    }
)
_VOID_TAGS = frozenset({"br", "hr", "img", "meta", "link", "input", "ri:attachment", "ri:page"})
_TEXT_TAG = "#text"


@dataclass(slots=True)
class _MutableNode:
    """Element or text node of the tolerant tree under construction."""

    tag: str
    attributes: dict[str, str]
    path: tuple[int, ...]
    # Only text nodes (`#text`) carry text; elements hold it in text children,
    # so text keeps its place between inline elements.
    text: list[str] = field(default_factory=list)
    children: list[_MutableNode] = field(default_factory=list)


class _TreeParser(HTMLParser):
    """``HTMLParser`` building a ``_MutableNode`` tree without validating nesting.

    Tag names are lower-cased and ``br`` becomes a newline.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _MutableNode("document", {}, (0,))
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open an element, or emit a newline for ``br``."""

        if tag.lower() == "br":
            self.handle_data("\n")
            return
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
        """Treat a self-closing tag as a start tag followed by its end."""

        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        """Close the nearest open element with this tag, dropping open descendants.

        An end tag with no matching open element is ignored.
        """

        normalized = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        """Append text, merging it into a preceding text node."""

        parent = self._stack[-1]
        if parent.children and parent.children[-1].tag == _TEXT_TAG:
            parent.children[-1].text.append(data)
            return
        parent.children.append(
            _MutableNode(_TEXT_TAG, {}, (*parent.path, len(parent.children)), text=[data])
        )

    def unknown_decl(self, data: str) -> None:
        """Keep CDATA sections as text, since the base parser would drop them."""

        # Storage format carries code and noformat macro bodies, and plain-text
        # link bodies, as CDATA; the base parser would drop them.
        if data.startswith("CDATA["):
            self.handle_data(data[len("CDATA[") :])


class ConfluenceMarkupParser:
    """Parser for storage-format XML and rendered HTML bodies.

    Stateless; each ``parse`` call builds a fresh tree.
    """

    def parse(self, value: str) -> ConfluenceNode:
        """Parse a storage-format or rendered HTML body into a node tree.

        Args:
            value: Page body markup.

        Returns:
            Root node of the parsed tree.

        Raises:
            ConfluenceNormalizationError: If the markup is malformed or has no element and
                no non-blank text.
        """

        parser = _TreeParser()
        try:
            parser.feed(value)
            parser.close()
        except (AssertionError, ValueError) as exc:
            raise ConfluenceNormalizationError("Confluence markup body is malformed") from exc
        if not any(
            child.tag != _TEXT_TAG or "".join(child.text).strip() for child in parser.root.children
        ):
            raise ConfluenceNormalizationError("Confluence markup body is empty")
        return self._node(parser.root)

    def _node(self, value: _MutableNode) -> ConfluenceNode:
        """Convert a mutable node and its subtree into a ``ConfluenceNode``.

        The source id is the first of ``data-local-id``, ``data-node-id``, ``id`` and
        ``ac:macro-id``, else ``markup:<path>``. Macro parameter children are dropped.
        """

        tag = value.tag
        attributes: dict[str, object] = dict(value.attributes)
        source_id = (
            value.attributes.get("data-local-id")
            or value.attributes.get("data-node-id")
            or value.attributes.get("id")
            or value.attributes.get("ac:macro-id")
            or f"markup:{'.'.join(str(part) for part in value.path)}"
        )
        # TODO: bound the nesting depth; deeply nested markup raises RecursionError.
        children = tuple(self._node(child) for child in value.children)
        kind = self._kind(value)
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            attributes["level"] = int(tag[1])
        if tag in {"ul", "ol"}:
            attributes["ordered"] = tag == "ol"
        if tag == "ac:structured-macro":
            macro_name = value.attributes.get("ac:name", "unknown")
            attributes["macro_name"] = macro_name
            attributes["parameters"] = self._macro_parameters(value)
            if self._is_include_macro(macro_name):
                attributes.update(self._include_target(value))
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
        """Map a tag to a node kind.

        Panel classes override all but heading, parameter and details tags; unknown tags
        are ``group``.
        """

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
        if tag in _INLINE_TAGS:
            return "inline"
        return _TAG_KINDS.get(tag, "group")

    @staticmethod
    def _macro_parameters(value: _MutableNode) -> dict[str, str]:
        """Collect a macro's text ``ac:parameter`` values, skipping blank names and values."""

        parameters: dict[str, str] = {}
        for child in value.children:
            if child.tag != "ac:parameter":
                continue
            name = child.attributes.get("ac:name", "")
            parameter_value = "".join(
                part for node in child.children if node.tag == _TEXT_TAG for part in node.text
            ).strip()
            if name and parameter_value:
                parameters[name] = parameter_value
        return parameters

    @staticmethod
    def _is_include_macro(value: str) -> bool:
        """Return whether a macro name is ``include`` or ``excerpt-include``."""

        return value.strip().casefold().replace("_", "-") in {
            "include",
            "excerpt-include",
        }

    @classmethod
    def _include_target(cls, value: _MutableNode) -> dict[str, str]:
        """Return the target page id, title and space key of an include macro, or ``{}``."""

        page = cls._first_descendant(value, "ri:page")
        if page is None:
            return {}
        return {
            "include_target_page_id": page.attributes.get("ri:content-id", "").strip(),
            "include_target_title": page.attributes.get("ri:content-title", "").strip(),
            "include_target_space_key": page.attributes.get("ri:space-key", "").strip(),
        }

    @classmethod
    def _first_descendant(
        cls,
        value: _MutableNode,
        tag: str,
    ) -> _MutableNode | None:
        """Return the first descendant with this tag in document order, or None."""

        for child in value.children:
            if child.tag == tag:
                return child
            descendant = cls._first_descendant(child, tag)
            if descendant is not None:
                return descendant
        return None
