"""A forgiving HTML tree builder on the standard library parser.

Source HTML is rarely well formed: paragraphs and list items are left open,
cells are never closed, and stray end tags appear. The builder applies the
few implicit-close rules that matter for structure and ignores the rest, so
a renderer always receives a proper tree. Namespaced tags such as
Confluence's ``ac:structured-macro`` keep their names, and CDATA sections
become text, which is how storage-format code macros carry their bodies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_P_CLOSERS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "dl",
        "fieldset",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "ul",
    }
)
_INLINE = frozenset(
    {"a", "abbr", "b", "code", "em", "font", "i", "s", "small", "span", "strong", "sub", "sup", "u"}
)
# Opening one of these closes the nearest open sibling, unless a boundary
# element (the enclosing container) is reached first.
_SIBLING_CLOSE: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "li": (frozenset({"li"}), frozenset({"ul", "ol"})),
    "dt": (frozenset({"dt", "dd"}), frozenset({"dl"})),
    "dd": (frozenset({"dt", "dd"}), frozenset({"dl"})),
    "tr": (frozenset({"tr"}), frozenset({"table", "thead", "tbody", "tfoot"})),
    "td": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "th": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "thead": (frozenset({"thead", "tbody", "tfoot"}), frozenset({"table"})),
    "tbody": (frozenset({"thead", "tbody", "tfoot"}), frozenset({"table"})),
    "tfoot": (frozenset({"thead", "tbody", "tfoot"}), frozenset({"table"})),
}
MAX_DEPTH = 96
"""Deeper elements are flattened into their ancestor: real documents never nest
this far, and rendering recurses a few frames per level."""


@dataclass(eq=False)
class HtmlNode:
    """One element; children are nodes or text strings, in document order."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[HtmlNode | str] = field(default_factory=list)

    def get(self, name: str, default: str = "") -> str:
        return self.attrs.get(name, default)

    def elements(self) -> list[HtmlNode]:
        return [child for child in self.children if isinstance(child, HtmlNode)]

    def text(self) -> str:
        """All descendant text, unmodified."""

        return "".join(child if isinstance(child, str) else child.text() for child in self.children)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("#root")
        self._stack: list[HtmlNode] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._implicit_close(tag)
        node = HtmlNode(tag, {name: value or "" for name, value in attrs})
        self._stack[-1].children.append(node)
        if tag not in VOID_TAGS and len(self._stack) < MAX_DEPTH:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._implicit_close(tag)
        self._stack[-1].children.append(HtmlNode(tag, {name: value or "" for name, value in attrs}))

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self._stack) - 1, 0, -1):
            if self._stack[depth].tag == tag:
                del self._stack[depth:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)

    def unknown_decl(self, data: str) -> None:
        if data.startswith("CDATA["):
            self._stack[-1].children.append(data[len("CDATA[") :])

    def _implicit_close(self, tag: str) -> None:
        if tag in _P_CLOSERS:
            self._close_open_paragraph()
        rule = _SIBLING_CLOSE.get(tag)
        if rule is None:
            return
        closes, boundary = rule
        for depth in range(len(self._stack) - 1, 0, -1):
            current = self._stack[depth].tag
            if current in boundary:
                return
            if current in closes:
                del self._stack[depth:]
                return

    def _close_open_paragraph(self) -> None:
        for depth in range(len(self._stack) - 1, 0, -1):
            current = self._stack[depth].tag
            if current == "p":
                del self._stack[depth:]
                return
            if current not in _INLINE:
                return


def parse_html(markup: str) -> HtmlNode:
    """Parse ``markup`` into a tree rooted at a synthetic ``#root`` node."""

    builder = _TreeBuilder()
    builder.feed(markup)
    builder.close()
    return builder.root
