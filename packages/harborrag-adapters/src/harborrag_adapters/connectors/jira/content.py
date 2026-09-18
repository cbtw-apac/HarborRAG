"""Text extraction and rendering for JIRA issue payloads."""

from __future__ import annotations

import re
from typing import Any

from harborrag_adapters.connectors.attachments.processing import AttachmentMetadata
from harborrag_adapters.parsers.common.normalization import compact_text, html_to_text

from .html_to_markdown import html_to_markdown
from .schemas import (
    JiraCustomFieldKind,
    JiraCustomFieldMetadata,
    JiraFieldContext,
)

CUSTOM_FIELD_PREFIX = "customfield_"


def build_raw_content(
    issue: dict[str, Any],
    *,
    comments: list[dict[str, Any]] | None = None,
    attachments: list[AttachmentMetadata] | None = None,
    include_attachment_text: bool = True,
) -> str:
    """Render a JIRA issue and optional child data as readable plain text."""
    fields = issue.get("fields", {})
    rendered_fields = issue.get("renderedFields", {}) or {}
    lines = [
        f"# {issue.get('key')} {fields.get('summary') or ''}".strip(),
        "",
        f"Type: {_name(fields.get('issuetype')) or ''}".strip(),
        f"Status: {_name(fields.get('status')) or ''}".strip(),
        f"Priority: {_name(fields.get('priority')) or ''}".strip(),
        "",
        "## Description",
        # Prefer rendered HTML (if available) so we preserve tables/formatting
        field_text(rendered_fields.get("description") or fields.get("description")),
    ]

    custom_field_lines = [
        f"{field.name}: {field.text}".strip()
        for field in custom_field_metadata(issue)
        if field.text
    ]
    if custom_field_lines:
        lines.extend(["", "## Custom Fields", *custom_field_lines])

    if comments:
        lines.extend(["", "## Comments"])
        for comment in comments:
            author = _display_name(comment.get("author") or {})
            body = field_text(comment.get("body") or comment.get("renderedBody"))
            lines.append(f"{author}: {body}".strip(": "))

    processed = [item for item in attachments or [] if item.text]
    if processed and include_attachment_text:
        lines.extend(["", "## Attachments"])
        for attachment in processed:
            lines.extend((f"### {attachment.title}", attachment.text or ""))

    return compact_text("\n".join(lines))


def custom_field_metadata(issue: dict[str, Any]) -> list[JiraCustomFieldMetadata]:
    """Normalize custom fields with display names, schemas, and rendered text."""
    fields = issue.get("fields", {})
    names = issue.get("names", {}) or {}
    schemas = issue.get("schema", {}) or {}
    rendered_fields = issue.get("renderedFields", {}) or {}
    project = fields.get("project") if isinstance(fields.get("project"), dict) else {}
    issue_type = fields.get("issuetype") if isinstance(fields.get("issuetype"), dict) else {}
    context = JiraFieldContext(
        project_id=_optional_text(project.get("id")),
        project_key=_optional_text(project.get("key")),
        issue_type_id=_optional_text(issue_type.get("id")),
        issue_type_name=_optional_text(issue_type.get("name")),
    )

    values: list[JiraCustomFieldMetadata] = []
    for field_id, value in fields.items():
        if not str(field_id).startswith(CUSTOM_FIELD_PREFIX):
            continue
        schema = schemas.get(field_id) or {}
        rendered_value = rendered_fields.get(field_id)
        text_source = rendered_value if rendered_value not in (None, "") else value
        text = field_text(text_source)
        values.append(
            JiraCustomFieldMetadata(
                field_id=str(field_id),
                name=str(names.get(field_id) or field_id),
                schema_type=schema.get("type") if isinstance(schema, dict) else None,
                custom_type=schema.get("custom") if isinstance(schema, dict) else None,
                value=value,
                text=text,
                value_kind=_custom_field_kind(
                    name=str(names.get(field_id) or field_id),
                    schema=schema if isinstance(schema, dict) else {},
                    value=value,
                    text=text,
                ),
                context=context,
            )
        )
    return values


def _custom_field_kind(
    *,
    name: str,
    schema: dict[str, Any],
    value: Any,
    text: str,
) -> JiraCustomFieldKind:
    schema_type = str(schema.get("type") or "").casefold()
    custom_type = str(schema.get("custom") or "").casefold()
    normalized_name = " ".join(name.casefold().replace("_", " ").split())
    if schema_type == "number" or (isinstance(value, (int, float)) and not isinstance(value, bool)):
        return JiraCustomFieldKind.NUMBER
    if schema_type == "boolean" or isinstance(value, bool):
        return JiraCustomFieldKind.BOOLEAN
    if schema_type in {"date", "datetime"} or "datepicker" in custom_type:
        return JiraCustomFieldKind.DATE
    if "user" in custom_type:
        return JiraCustomFieldKind.USER
    if _is_option_value(value, schema_type=schema_type, custom_type=custom_type):
        return JiraCustomFieldKind.OPTION
    prose_name = any(
        marker in normalized_name
        for marker in ("acceptance criteria", "description", "environment", "reproduction")
    )
    if text and (
        prose_name
        or "textarea" in custom_type
        or "text field (multi-line)" in custom_type
        or "\n" in text
        or len(text) >= 80
    ):
        return JiraCustomFieldKind.PROSE
    return JiraCustomFieldKind.ATTRIBUTE


def _is_option_value(value: Any, *, schema_type: str, custom_type: str) -> bool:
    if "option" in custom_type or schema_type in {"option", "array"}:
        return True
    if isinstance(value, dict):
        return "value" in value and not {"type", "content"} <= set(value)
    if isinstance(value, list) and value:
        return all(isinstance(item, dict) and "value" in item for item in value)
    return False


def _optional_text(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    return str(value).strip()


def field_text(value: Any) -> str:
    """Extract readable text from JIRA strings, HTML, ADF, lists, or scalars."""
    if value is None:
        return ""
    if isinstance(value, str):
        return _field_text_from_string(value)
    if isinstance(value, dict):
        # If this looks like Atlassian Document Format (ADF), attempt to
        # convert it to Markdown (preserving tables) before falling back
        # to a plain-text ADF walk.
        return _field_text_from_dict(value)
    if isinstance(value, list):
        return compact_text("\n".join(field_text(item) for item in value))
    return compact_text(str(value))


def _field_text_from_string(value: str) -> str:
    """Handle string values: prefer HTML->Markdown for tables, else plain text."""
    if "<" in value and ">" in value:
        try:
            md = html_to_markdown(value)
            if md and "|" in md and "---" in md:
                return md
        except Exception:
            pass
        return html_to_text(value)
    return compact_text(value)


def _field_text_from_dict(value: dict[str, Any]) -> str:
    """Handle dict-like values including ADF, objects, and nested structures."""
    try:
        if value.get("type") == "doc" or isinstance(value.get("content"), list):
            md = _adf_to_markdown(value)
            if md and md.strip():
                return md.strip()
    except Exception:
        # fall through to text-only extraction on any failure
        pass
    adf_text = compact_text("".join(_walk_adf(value)))
    if adf_text:
        return adf_text
    for key in ("displayName", "name", "value", "key", "emailAddress"):
        if value.get(key):
            return compact_text(str(value[key]))
    nested_parts = [field_text(item) for item in value.values()]
    return compact_text("\n".join(part for part in nested_parts if part))


def _walk_adf(node: Any) -> list[str]:
    """Walk Atlassian Document Format nodes into plain-text fragments."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        list_parts: list[str] = []
        for child in node:
            list_parts.extend(_walk_adf(child))
        return list_parts
    if not isinstance(node, dict):
        return []

    node_type = node.get("type")
    if node_type == "text":
        return [str(node.get("text") or "")]
    if node_type == "hardBreak":
        return ["\n"]

    node_parts: list[str] = []
    for child in node.get("content", []) or []:
        node_parts.extend(_walk_adf(child))
    if node_type in {"paragraph", "heading", "listItem"} and node_parts:
        node_parts.append("\n")
    return node_parts


def _adf_to_markdown(node: Any) -> str:
    """Render a subset of ADF to Markdown, with table support.

    This implements minimal ADF handling sufficient to render tables and
    paragraphs into readable Markdown. It is conservative and falls back
    to plain-text when structures are unfamiliar.
    """
    parts: list[str] = []

    def render(n: Any) -> str:
        if isinstance(n, str):
            return n
        if isinstance(n, list):
            return "".join(render(child) for child in n)
        if not isinstance(n, dict):
            return ""
        t = n.get("type")
        if t == "paragraph":
            return compact_text("".join(_walk_adf(n))) + "\n\n"
        if t == "heading":
            level = (n.get("attrs") or {}).get("level") or 1
            try:
                level = int(level)
            except Exception:
                level = 1
            text = compact_text("".join(_walk_adf(n)))
            return f"{('#' * max(1, min(level, 6)))} {text}\n\n" if text else ""
        if t == "table":
            return _adf_table_to_markdown(n) + "\n\n"
        # Generic: render children
        return "".join(render(child) for child in n.get("content", []) or [])

    parts.append(render(node))
    return _compact_md("".join(parts))


def _adf_table_to_markdown(table_node: dict[str, Any]) -> str:
    rows, header_row = _extract_adf_table_rows(table_node)
    if not rows:
        return ""
    return _format_md_table(rows, header_row)


def _parse_span(attrs: dict[str, Any], key: str) -> int:
    try:
        return max(1, int(attrs.get(key) or 1))
    except (TypeError, ValueError):
        return 1


def _extract_adf_table_row(
    row: dict[str, Any], row_spans: dict[int, int]
) -> tuple[list[str], bool]:
    """Render one ADF tableRow into a grid-aligned list of cell strings.

    `row_spans` (column index -> remaining rows to blank-fill) is mutated in
    place so a cell's rowspan carries a blank placeholder into later rows.
    """
    cell_nodes = [c for c in row.get("content", []) or [] if isinstance(c, dict)]
    out_row: list[str] = []
    col = 0
    idx = 0
    row_has_header = False
    while idx < len(cell_nodes) or col in row_spans:
        if row_spans.get(col, 0) > 0:
            out_row.append("")
            row_spans[col] -= 1
            if row_spans[col] <= 0:
                del row_spans[col]
            col += 1
            continue
        cell = cell_nodes[idx]
        idx += 1
        attrs = cell.get("attrs") or {}
        colspan = _parse_span(attrs, "colspan")
        rowspan = _parse_span(attrs, "rowspan")
        parts: list[str] = []
        for child in cell.get("content", []) or []:
            parts.extend(_walk_adf(child))
        if cell.get("type") == "tableHeader":
            row_has_header = True
        out_row.append(_escape_table_cell(compact_text("".join(parts))))
        out_row.extend([""] * (colspan - 1))
        if rowspan > 1:
            for spanned_col in range(col, col + colspan):
                row_spans[spanned_col] = rowspan - 1
        col += colspan
    return out_row, row_has_header


def _extract_adf_table_rows(table_node: dict[str, Any]) -> tuple[list[list[str]], int | None]:
    """Return normalized rows and header_row index (or None).

    Accounts for `attrs.rowspan`/`attrs.colspan` on each cell so that a
    merged cell occupies its full grid footprint: spanned columns get a
    blank placeholder and rows covered by a rowspan skip the occupied
    column, keeping later cells aligned under the correct header.
    """
    rows: list[list[str]] = []
    header_row: int | None = None
    row_spans: dict[int, int] = {}
    for row in table_node.get("content", []) or []:
        if not isinstance(row, dict) or row.get("type") != "tableRow":
            continue
        out_row, row_has_header = _extract_adf_table_row(row, row_spans)
        if out_row:
            rows.append(out_row)
            if header_row is None and row_has_header:
                header_row = len(rows) - 1
    return rows, header_row


def _format_md_table(rows: list[list[str]], header_row: int | None) -> str:
    max_cols = max(len(r) for r in rows)
    norm = [r + [""] * (max_cols - len(r)) for r in rows]
    if header_row is not None and 0 <= header_row < len(norm):
        header = norm[header_row]
        data = norm[:header_row] + norm[header_row + 1 :]
    else:
        header = [f"Column {i + 1}" for i in range(max_cols)]
        data = norm
    sep = ["---"] * max_cols
    md_lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(sep) + " |"]
    for row in data:
        md_lines.append("| " + " | ".join(row) + " |")
    return "\n".join(md_lines)


def _escape_table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


def _compact_md(value: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def _name(value: Any) -> str | None:
    if not isinstance(value, dict) or not value.get("name"):
        return None
    return str(value["name"])


def _display_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    name = value.get("displayName") or value.get("name") or value.get("emailAddress")
    return str(name) if name else None
