"""Small HTML -> Markdown helpers used by the Jira connector.

Only a lightweight subset of Confluence's test helper is required here:
- preserve `<table>` content as GitHub-flavored Markdown tables
- escape pipes and inline newlines inside cells
- fall back to plain-text extraction when BeautifulSoup isn't available
"""

from __future__ import annotations

import re

try:
    from bs4 import BeautifulSoup, Tag
except Exception:  # pragma: no cover - optional dependency runtime
    BeautifulSoup = None  # type: ignore
    Tag = object  # type: ignore

_NL_RE = re.compile(r"\n{2,}")


def html_to_markdown(html: str | bytes) -> str:
    """Convert HTML to readable Markdown only when tables are present.

    If BeautifulSoup is unavailable or no `<table>` exists, return the
    original HTML text flattened via simple whitespace compaction (caller
    can pass through to `html_to_text` instead).
    """
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    if BeautifulSoup is None:
        # no parser available; caller should fallback
        return _compact_text(html)

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        return _compact_text(soup.get_text("\n", strip=True))

    # Replace tables with markdown-rendered equivalents
    for table in tables:
        md = _render_table(table)
        table.replace_with(md)

    # Get resulting text, compact excessive blank lines
    out = soup.get_text("\n", strip=True)
    out = _NL_RE.sub("\n\n", out)
    return out.strip()


def _render_table(table: Tag) -> str:
    # Collect rows
    rows: list[list[str]] = []
    row_has_th: list[bool] = []
    for tr in table.find_all("tr"):
        cells = []
        has_th = False
        for cell in tr.find_all(["th", "td"]):
            text = _cell_text(cell)
            cells.append(_escape_cell(text))
            if cell.name == "th":
                has_th = True
        if cells:
            rows.append(cells)
            row_has_th.append(has_th)
    if not rows:
        return ""
    # Ensure rectangular grid
    max_cols = max(len(r) for r in rows)
    norm = [r + [""] * (max_cols - len(r)) for r in rows]
    # If first emitted row is header-like (contains <th>) use it, else synthesize
    has_th = row_has_th[0]
    if has_th:
        header = norm[0]
        data = norm[1:]
    else:
        header = [f"Column {i + 1}" for i in range(max_cols)]
        data = norm
    sep = ["---"] * max_cols
    md_rows = ["| " + " | ".join(header) + " |", "| " + " | ".join(sep) + " |"]
    for row in data:
        md_rows.append("| " + " | ".join(row) + " |")
    return "\n".join(md_rows)


def _cell_text(cell: Tag) -> str:
    # prefer textual content; preserve explicit <br> boundaries as newlines
    parts: list[str] = []
    for child in cell.children:
        # If this child is an explicit <br> tag, preserve a newline marker.
        name = getattr(child, "name", "")
        if isinstance(name, str) and name.lower() == "br":
            parts.append("\n")
            continue
        if hasattr(child, "get_text"):
            parts.append(str(child.get_text(" ", strip=True)))
        else:
            parts.append(str(child).strip())

    # Build a string preserving newline tokens while compacting other whitespace
    out = ""
    for p in parts:
        if p == "\n":
            if not out.endswith("\n"):
                out += "\n"
            continue
        chunk = p.strip()
        if not chunk:
            continue
        if not out or out.endswith("\n"):
            out += chunk
        else:
            out += " " + chunk
    return out


def _escape_cell(value: str) -> str:
    # escape pipe and replace newlines inside cells with <br>
    v = value.replace("|", "\\|")
    v = v.replace("\n", "<br>")
    return v


def _compact_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    compact: list[str] = []
    for line in lines:
        if not line:
            if compact and compact[-1]:
                compact.append("")
            continue
        compact.append(line)
    return "\n".join(compact).strip()
