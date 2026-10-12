"""Deterministic HTML to Markdown, keeping the structure the chunker splits on.

Headings stay ATX headings, lists keep their nesting, tables stay GitHub
tables (one per source table, never split), and code keeps its language.
The same input always renders to the same bytes, so content hashes stay
stable across runs.

Rendering works on *parts*: an element renders to inline text or to a block.
A block container groups consecutive inline parts into one paragraph, tidies
its whitespace once, and joins blocks with a blank line. Indentation for
lists and quotes is applied after tidying, so it is never collapsed.

Text is not Markdown-escaped, except ``|`` inside table cells: escaping would
put backslashes into identifiers like ``customer_id`` for no retrieval gain.

Vendors extend the renderer through ``handlers``: Confluence storage macros
and SharePoint canvas web parts map their own tags to inline text or
:class:`Block` output.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .html_tree import HtmlNode, parse_html


@dataclass(frozen=True, slots=True)
class Block:
    """Block-level output from a handler; plain ``str`` output is inline."""

    text: str


type Rendered = Block | str
type ElementHandler = Callable[[HtmlNode, RenderContext], Rendered]

_WHITESPACE = re.compile(r"[ \t\n\r\f\v\u00a0]+")
_SPACES = re.compile(r"[ \t\u00a0]+")
_DROPPED = frozenset({"head", "noscript", "script", "style", "template", "title"})
_LANGUAGE = re.compile(r"(?:^|\s)(?:language|lang)-([A-Za-z0-9_+#.-]+)")


class RenderContext:
    """Rendering state passed to handlers."""

    def __init__(self, handlers: Mapping[str, ElementHandler]) -> None:
        self._handlers = handlers
        self.in_pre = False
        self.in_cell = False

    def render(self, node: HtmlNode | str) -> list[Rendered]:
        if isinstance(node, str):
            return [node if self.in_pre else _WHITESPACE.sub(" ", node)]
        if node.tag in _DROPPED:
            return []
        handler = self._handlers.get(node.tag)
        if handler is None:
            return self.parts(node)
        return [handler(node, self)]

    def parts(self, node: HtmlNode) -> list[Rendered]:
        """Children rendered in order, unmerged: the transparent-element behaviour."""

        out: list[Rendered] = []
        for child in node.children:
            out.extend(self.render(child))
        return out

    def blocks(self, node: HtmlNode) -> list[str]:
        """Children as a list of tidy blocks; inline runs become paragraphs."""

        blocks: list[str] = []
        run: list[str] = []
        for part in self.parts(node):
            if isinstance(part, Block):
                _flush(run, blocks)
                if part.text.strip():
                    blocks.append(part.text)
            else:
                run.append(part)
        _flush(run, blocks)
        return blocks

    def inline(self, node: HtmlNode) -> str:
        """Children as one tidy run of inline text; nested blocks become lines."""

        parts = self.parts(node)
        if any(isinstance(part, Block) for part in parts):
            return tidy("\n".join(_text(part) for part in parts))
        return tidy("".join(_text(part) for part in parts))

    def raw_text(self, node: HtmlNode) -> str:
        """Descendant text with ``<br>`` as newlines and nothing collapsed."""

        out: list[str] = []
        for child in node.children:
            if isinstance(child, str):
                out.append(child)
            elif child.tag == "br":
                out.append("\n")
            else:
                out.append(self.raw_text(child))
        return "".join(out)


def _text(part: Rendered) -> str:
    return part.text if isinstance(part, Block) else part


def tidy(text: str) -> str:
    """Collapse spaces, strip every line, and drop leading and trailing blank lines."""

    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(lines).strip("\n")


def _flush(run: list[str], blocks: list[str]) -> None:
    text = tidy("".join(run))
    run.clear()
    if text:
        blocks.append(text)


def indent(text: str, first: str, rest: str) -> str:
    """Prefix the first line with ``first`` and every later non-empty line with ``rest``."""

    lines = text.split("\n")
    out = [first + lines[0]]
    out.extend(rest + line if line else "" for line in lines[1:])
    return "\n".join(out)


def _heading(node: HtmlNode, ctx: RenderContext) -> Rendered:
    text = " ".join(ctx.inline(node).split("\n"))
    if not text:
        return ""
    return Block(f"{'#' * int(node.tag[1])} {text}")


def _block(node: HtmlNode, ctx: RenderContext) -> Rendered:
    return Block("\n\n".join(ctx.blocks(node)))


def _line_break(node: HtmlNode, ctx: RenderContext) -> Rendered:
    return "\n"


def _rule(node: HtmlNode, ctx: RenderContext) -> Rendered:
    return Block("---")


def _wrap(marker: str) -> ElementHandler:
    def render(node: HtmlNode, ctx: RenderContext) -> Rendered:
        raw = "".join(_text(part) for part in ctx.parts(node))
        text = tidy(raw)
        if not text:
            return raw
        # Emphasis markers must touch the text, so surrounding spaces move outside.
        lead = " " if raw[:1].isspace() else ""
        trail = " " if raw[-1:].isspace() else ""
        return f"{lead}{marker}{text}{marker}{trail}"

    return render


def _fence_for(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _code(node: HtmlNode, ctx: RenderContext) -> Rendered:
    if ctx.in_pre:
        return ctx.raw_text(node)
    text = _WHITESPACE.sub(" ", node.text())
    if not text.strip():
        return text
    ticks = "`" * (max((len(r) for r in re.findall(r"`+", text)), default=0) + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{padding}{text}{padding}{ticks}"


def _language(node: HtmlNode) -> str:
    for candidate in (node, *node.elements()):
        match = _LANGUAGE.search(candidate.get("class"))
        if match:
            return match.group(1).lower()
        if candidate.get("data-language"):
            return candidate.get("data-language").lower()
    return ""


def code_block(text: str, language: str = "") -> Block:
    """A fenced code block whose fence is longer than any backtick run inside."""

    body = text.strip("\n")
    fence = _fence_for(body)
    return Block(f"{fence}{language}\n{body}\n{fence}")


def _pre(node: HtmlNode, ctx: RenderContext) -> Rendered:
    previous, ctx.in_pre = ctx.in_pre, True
    try:
        text = ctx.raw_text(node)
    finally:
        ctx.in_pre = previous
    if ctx.in_cell:
        return " ".join(text.split())
    return code_block("\n".join(line.rstrip() for line in text.split("\n")), _language(node))


def _link(node: HtmlNode, ctx: RenderContext) -> Rendered:
    text = ctx.inline(node).replace("\n", " ")
    href = node.get("href").strip()
    if not href or href.startswith("javascript:"):
        return text
    if not text or text == href:
        return f"<{href}>"
    return f"[{text}]({href.replace(' ', '%20')})"


def _image(node: HtmlNode, ctx: RenderContext) -> Rendered:
    alt = " ".join(node.get("alt").split())
    src = node.get("src").strip()
    if not src or src.startswith("data:"):
        return alt
    return f"![{alt}]({src.replace(' ', '%20')})"


def _quote(node: HtmlNode, ctx: RenderContext) -> Rendered:
    body = "\n\n".join(ctx.blocks(node))
    if not body:
        return ""
    return Block("\n".join(f"> {line}" if line else ">" for line in body.split("\n")))


def _list(node: HtmlNode, ctx: RenderContext) -> Rendered:
    ordered = node.tag == "ol"
    start = int(node.get("start")) if ordered and node.get("start").isdigit() else 1
    items: list[str] = []
    for child in node.children:
        if isinstance(child, HtmlNode) and child.tag == "li":
            marker = f"{start + len(items)}. " if ordered else "- "
            body = "\n".join(ctx.blocks(child)) or ""
            items.append(indent(body, marker, " " * len(marker)))
        elif isinstance(child, HtmlNode):
            # A list nested directly in a list belongs to the previous item.
            extra = "\n".join(_as_blocks(ctx.render(child)))
            if extra and items:
                items[-1] += "\n" + indent(extra, "  ", "  ")
            elif extra:
                items.append(extra)
    if ctx.in_cell:
        return "; ".join(item.replace("\n", " ") for item in items)
    return Block("\n".join(items))


def _as_blocks(parts: list[Rendered]) -> list[str]:
    texts = (part.text if isinstance(part, Block) else tidy(part) for part in parts)
    return [text for text in texts if text.strip()]


def _definition_term(node: HtmlNode, ctx: RenderContext) -> Rendered:
    text = ctx.inline(node)
    return Block(f"**{text}**") if text else ""


def _table(node: HtmlNode, ctx: RenderContext) -> Rendered:
    previous, ctx.in_cell = ctx.in_cell, True
    try:
        rows = [[_cell(cell, ctx) for cell in _cells(row)] for row in _rows(node)]
    finally:
        ctx.in_cell = previous
    rows = [row for row in rows if row]
    if not rows:
        return ""
    if ctx.in_cell:
        # A table inside a cell cannot stay a table: flatten it to text.
        return " " + "<br>".join(" / ".join(row) for row in rows) + " "
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    lines = [_row_line(padded[0]), _row_line(["---"] * width)]
    lines.extend(_row_line(row) for row in padded[1:])
    return Block("\n".join(lines))


def _row_line(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _rows(table: HtmlNode) -> list[HtmlNode]:
    rows: list[HtmlNode] = []
    for child in table.elements():
        if child.tag == "tr":
            rows.append(child)
        elif child.tag in ("thead", "tbody", "tfoot"):
            rows.extend(row for row in child.elements() if row.tag == "tr")
    return rows


def _cells(row: HtmlNode) -> list[HtmlNode]:
    cells: list[HtmlNode] = []
    for cell in row.elements():
        if cell.tag in ("td", "th"):
            span = cell.get("colspan")
            cells.append(cell)
            extra = min(int(span), 64) - 1 if span.isdigit() else 0
            cells.extend(HtmlNode("td") for _ in range(max(0, extra)))
    return cells


def _cell(cell: HtmlNode, ctx: RenderContext) -> str:
    text = "<br>".join(ctx.blocks(cell))
    return text.replace("\n", "<br>").replace("|", "\\|")


def default_handlers() -> dict[str, ElementHandler]:
    """The tag handlers the renderer starts from; vendors override or extend them."""

    handlers: dict[str, ElementHandler] = {f"h{level}": _heading for level in range(1, 7)}
    for tag in (
        "address", "article", "aside", "body", "center", "dd", "details", "div",
        "dl", "figcaption", "figure", "footer", "header", "html", "main", "nav",
        "p", "section", "summary",
    ):  # fmt: skip
        handlers[tag] = _block
    handlers.update(
        {
            "a": _link,
            "b": _wrap("**"),
            "blockquote": _quote,
            "br": _line_break,
            "code": _code,
            "del": _wrap("~~"),
            "dt": _definition_term,
            "em": _wrap("*"),
            "hr": _rule,
            "i": _wrap("*"),
            "img": _image,
            "kbd": _code,
            "ol": _list,
            "pre": _pre,
            "s": _wrap("~~"),
            "strike": _wrap("~~"),
            "strong": _wrap("**"),
            "table": _table,
            "tt": _code,
            "ul": _list,
        }
    )
    return handlers


class HtmlMarkdownRenderer:
    """Render HTML to Markdown; same input, same bytes."""

    def __init__(self, handlers: Mapping[str, ElementHandler] | None = None) -> None:
        self._handlers = {**default_handlers(), **(handlers or {})}

    def render(self, html: str) -> str:
        return self.render_tree(parse_html(html))

    def render_tree(self, root: HtmlNode) -> str:
        blocks = RenderContext(self._handlers).blocks(root)
        return "\n\n".join(blocks) + "\n" if blocks else ""
