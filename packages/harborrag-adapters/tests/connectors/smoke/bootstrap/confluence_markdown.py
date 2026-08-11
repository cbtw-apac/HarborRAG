"""Readable Markdown rendering for Confluence's server-expanded HTML preview."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString, Tag

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


def confluence_html_to_markdown(value: str) -> str:
    """Convert rendered Confluence HTML to deterministic, readable Markdown."""

    soup = BeautifulSoup(value, "html.parser")
    for node in soup.find_all(_SKIP_TAGS):
        node.decompose()
    for node in soup.select(".aui-icon, .expand-control-image"):
        node.decompose()
    return _compact_markdown(_render_fragment(soup))


def _render_fragment(root: BeautifulSoup | Tag) -> str:
    if isinstance(root, Tag) and root.name in _BLOCK_TAGS:
        return _render_block(root)
    blocks: list[str] = []
    for node in root.find_all(_BLOCK_TAGS):
        if _has_block_ancestor(node, root):
            continue
        rendered = _render_block(node)
        if rendered:
            blocks.append(rendered)
    if blocks:
        return "\n\n".join(blocks)
    return _normalize_inline(_render_inline(root))


def _has_block_ancestor(node: Tag, root: BeautifulSoup | Tag) -> bool:
    parent = node.parent
    while isinstance(parent, Tag) and parent is not root:
        if parent.name in _BLOCK_TAGS:
            return True
        parent = parent.parent
    return False


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
        if not isinstance(child, Tag):
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
        if isinstance(child, NavigableString):
            inline.append(str(child))
            continue
        if not isinstance(child, Tag):
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
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    if not rows:
        return ""
    grid: list[list[str | None]] = []
    header_flags: list[bool] = []
    nested_tables: list[Tag] = []
    for row_index, row in enumerate(rows):
        _ensure_grid(grid, row_index + 1, 1)
        cells = row.find_all(["th", "td"], recursive=False)
        header_flags.append(any(cell.name == "th" for cell in cells))
        column_index = 0
        for cell in cells:
            while column_index < len(grid[row_index]) and grid[row_index][column_index] is not None:
                column_index += 1
            row_span = _positive_span(cell.get("rowspan"))
            column_span = _positive_span(cell.get("colspan"))
            _ensure_grid(grid, row_index + row_span, column_index + column_span)
            text = _render_table_cell(cell)
            span_labels = [
                *([f"spans {row_span} rows"] if row_span > 1 else []),
                *([f"spans {column_span} columns"] if column_span > 1 else []),
            ]
            origin = f"{text} ({', '.join(span_labels)})" if span_labels else text
            for target_row in range(row_index, row_index + row_span):
                for target_column in range(column_index, column_index + column_span):
                    grid[target_row][target_column] = (
                        origin if target_row == row_index and target_column == column_index else ""
                    )
            column_index += column_span
            nested_tables.extend(
                nested for nested in cell.find_all("table") if nested.find_parent("table") is table
            )
    column_count = max(len(row) for row in grid)
    normalized = [
        [*(cell or "" for cell in row), *("" for _ in range(column_count - len(row)))]
        for row in grid
    ]
    if header_flags and header_flags[0]:
        header, data = normalized[0], normalized[1:]
    else:
        header = [f"Column {index + 1}" for index in range(column_count)]
        data = normalized
    markdown_rows = [header, ["---"] * column_count, *data]
    rendered = "\n".join(
        f"| {' | '.join(_escape_table_cell(cell) for cell in row)} |" for row in markdown_rows
    )
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
        if isinstance(child, Tag) and child.name == "table":
            continue
        if isinstance(child, Tag) and child.find_parent("table") is not cell.find_parent("table"):
            continue
        if isinstance(child, Tag) and child.name in {"ul", "ol"}:
            value = _render_list(child)
        else:
            value = _render_inline(child)
        if normalized := _normalize_inline(value):
            parts.append(normalized)
    return "\n".join(parts)


def _render_inline(node: object) -> str:
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node).replace("\xa0", " "))
    if not isinstance(node, (BeautifulSoup, Tag)):
        return ""
    if isinstance(node, Tag) and (node.name in _SKIP_TAGS or node.name == "table"):
        return ""
    children = "".join(_render_inline(child) for child in node.children)
    if not isinstance(node, Tag):
        return children
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


def _positive_span(value: object) -> int:
    try:
        return max(int(str(value or 1)), 1)
    except ValueError:
        return 1


def _ensure_grid(grid: list[list[str | None]], rows: int, columns: int) -> None:
    while len(grid) < rows:
        grid.append([])
    for row in grid:
        row.extend(None for _ in range(columns - len(row)))


def _escape_table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")


def _normalize_inline(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _compact_markdown(value: str) -> str:
    lines = [line.rstrip() for line in value.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
