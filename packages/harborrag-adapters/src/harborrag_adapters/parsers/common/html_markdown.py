"""Deterministic HTML-to-Markdown rendering that preserves table structure.

`html_to_text` flattens markup to visible text, which turns a table into one
cell per line: the rows, the columns, and therefore the meaning of every cell
are lost. Anything that survives into chunk context - a Jira comment holding a
status table, a rendered custom field, a Confluence page body - needs the grid
kept, so this module renders block structure (headings, lists, tables, code,
quotes) as Markdown instead.

Beautiful Soup is an optional parser dependency. Without it there is no HTML
tree to walk, so rendering degrades to `html_to_text` and reports that engine
rather than failing the parse.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypeGuard

from harborrag_adapters.parsers.common.markdown_tables import (
    TableCell,
    markdown_table,
    positive_span,
)
from harborrag_adapters.parsers.common.normalization import html_to_text_with_engine

if TYPE_CHECKING:
    from bs4 import BeautifulSoup, Tag

MARKDOWN_ENGINE = "beautifulsoup4/html.parser"

_BLOCK_TAGS = frozenset(
    {
        "blockquote",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "ol",
        "p",
        "pre",
        "table",
        "ul",
    }
)
_SKIP_TAGS = frozenset({"button", "head", "noscript", "script", "style"})


def html_to_markdown(
    html: str | bytes,
    *,
    drop_selectors: Sequence[str] = (),
) -> str:
    """Render HTML as Markdown, keeping tables as Markdown tables.

    `drop_selectors` removes provider chrome (icon spans, expand controls, and
    similar) before rendering, so no caller has to post-process the output.
    """
    return html_to_markdown_with_engine(html, drop_selectors=drop_selectors)[0]


def html_to_markdown_with_engine(
    html: str | bytes,
    *,
    drop_selectors: Sequence[str] = (),
) -> tuple[str, str]:
    """Render HTML as Markdown and report which backend produced it."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html_to_text_with_engine(html)

    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(_SKIP_TAGS):
        node.decompose()
    for selector in drop_selectors:
        for node in soup.select(selector):
            node.decompose()
    return _compact_markdown(_render_fragment(soup)), MARKDOWN_ENGINE


def _is_tag(node: Any) -> TypeGuard[Tag]:
    """Report whether a parsed node is an element rather than a string.

    Beautiful Soup's `NavigableString` subclasses `str` and carries no tag
    name, so this distinguishes both without importing bs4 at runtime - the
    library stays an optional dependency of this module.
    """
    return not isinstance(node, str) and getattr(node, "name", None) is not None


def _render_fragment(root: BeautifulSoup | Tag) -> str:
    """Render one subtree, in document order, as blocks separated by a blank line.

    Children are walked rather than searched for block tags, so text sitting
    loose beside a block - a Jira comment that opens with a sentence and then
    a table, say - keeps both its content and its position instead of being
    dropped in favour of the blocks around it.
    """
    if _is_tag(root) and root.name in _BLOCK_TAGS:
        return _render_block(root)

    blocks: list[str] = []
    inline_run: list[str] = []

    def flush_inline() -> None:
        if text := _normalize_inline("".join(inline_run)):
            blocks.append(text)
        inline_run.clear()

    for child in root.children:
        if not _is_tag(child):
            inline_run.append(_render_inline(child))
            continue
        if child.name in _BLOCK_TAGS:
            flush_inline()
            if rendered := _render_block(child):
                blocks.append(rendered)
            continue
        if child.find(_BLOCK_TAGS):
            flush_inline()
            if rendered := _render_fragment(child):
                blocks.append(rendered)
            continue
        inline_run.append(_render_inline(child))
    flush_inline()
    return "\n\n".join(blocks)


def _render_block(node: Tag) -> str:
    name = node.name
    if name and len(name) == 2 and name[0] == "h" and name[1].isdigit():
        level = min(max(int(name[1]), 1), 6)
        return f"{'#' * level} {_normalize_inline(_render_inline(node))}"
    if name == "p":
        return _normalize_inline(_render_inline(node))
    if name in {"ul", "ol"}:
        return _render_list(node)
    if name == "table":
        return _render_table(node)
    if name == "pre":
        code = node.get_text("\n", strip=False).strip("\n")
        return f"```text\n{code}\n```" if code else ""
    if name == "blockquote":
        text = _normalize_inline(_render_inline(node))
        return "\n".join(f"> {line}" for line in text.splitlines())
    if name == "hr":
        return "---"
    return _normalize_inline(_render_inline(node))


def _render_list(node: Tag, *, depth: int = 0) -> str:
    lines: list[str] = []
    item_number = 0
    for child in node.children:
        if not _is_tag(child):
            continue
        if child.name == "li":
            item_number += 1
            marker = f"{item_number}." if node.name == "ol" else "-"
            text, nested = _render_list_item(child, depth=depth)
            if text:
                lines.append(f"{'  ' * depth}{marker} {text}")
            lines.extend(nested)
            continue
        if child.name == "br":
            continue
        extra = _render_fragment(child)
        if extra:
            if lines and lines[-1]:
                lines.append("")
            lines.extend(("  " * depth) + line for line in extra.splitlines())
    return "\n".join(lines)


def _render_list_item(node: Tag, *, depth: int) -> tuple[str, list[str]]:
    inline: list[str] = []
    nested: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            inline.append(str(child))
            continue
        if not _is_tag(child):
            continue
        if child.name in {"ul", "ol"}:
            rendered = _render_list(child, depth=depth + 1)
            if rendered:
                nested.extend(rendered.splitlines())
        elif child.name in {"div", "table"} and child.find(_BLOCK_TAGS):
            rendered = _render_fragment(child)
            if rendered:
                nested.extend(("  " * (depth + 1)) + line for line in rendered.splitlines())
        else:
            inline.append(_render_inline(child))
    return _normalize_inline("".join(inline)), nested


def _render_table(table: Tag) -> str:
    """Render one `<table>`, with the tables nested inside its cells after it.

    A nested table cannot be expressed inside a Markdown cell, so each one is
    rendered as its own labelled table below the outer grid rather than being
    flattened into the cell that held it.
    """
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    if not rows:
        return ""

    cell_rows: list[list[TableCell]] = []
    nested_tables: list[Tag] = []
    for row in rows:
        cells = row.find_all(["th", "td"], recursive=False)
        cell_rows.append(
            [
                TableCell(
                    text=_render_table_cell(cell),
                    row_span=positive_span(cell.get("rowspan")),
                    column_span=positive_span(cell.get("colspan")),
                    header=cell.name == "th",
                )
                for cell in cells
            ]
        )
        for cell in cells:
            nested_tables.extend(
                nested for nested in cell.find_all("table") if nested.find_parent("table") is table
            )

    rendered = markdown_table(cell_rows)
    nested_rendered = [value for nested in nested_tables if (value := _render_table(nested))]
    if nested_rendered:
        rendered += "\n\n" + "\n\n".join(
            f"**Nested table {index}**\n\n{value}"
            for index, value in enumerate(nested_rendered, start=1)
        )
    return rendered


def _render_table_cell(cell: Tag) -> str:
    parts: list[str] = []
    for child in cell.children:
        if _is_tag(child) and child.name == "table":
            continue
        if _is_tag(child) and child.find_parent("table") is not cell.find_parent("table"):
            continue
        if _is_tag(child) and child.name in {"ul", "ol"}:
            value = _render_list(child)
        else:
            value = _render_inline(child)
        if normalized := _normalize_inline(value):
            parts.append(normalized)
    return "\n".join(parts)


def _render_inline(node: Any) -> str:
    if isinstance(node, str):
        return re.sub(r"\s+", " ", str(node).replace("\xa0", " "))
    if not _is_tag(node):
        return ""
    if node.name in _SKIP_TAGS or node.name == "table":
        return ""
    children = "".join(_render_inline(child) for child in node.children)
    content = _normalize_inline(children)
    return _render_inline_tag(node, content, children)


def _render_inline_tag(node: Tag, content: str, children: str) -> str:
    if node.name in {"strong", "b"} and content:
        return f"**{content}**"
    if node.name in {"em", "i"} and content:
        return f"*{content}*"
    if node.name == "code" and content:
        return f"`{content.replace('`', '\\`')}`"
    if node.name == "a" and content:
        href = str(node.get("href") or "").strip()
        return f"[{content}]({href})" if href else content
    if node.name == "img":
        alt = str(node.get("alt") or "image").strip()
        src = str(node.get("src") or "").strip()
        return f"![{alt}]({src})" if src else alt
    if node.name == "br":
        return "\n"
    if node.name in {"div", "li", "p"}:
        return f"{children}\n"
    return children


def _normalize_inline(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _compact_markdown(value: str) -> str:
    lines = [line.rstrip() for line in value.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
