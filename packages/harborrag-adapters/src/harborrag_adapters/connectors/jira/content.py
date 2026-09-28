"""Text extraction and rendering for JIRA issue payloads."""

from __future__ import annotations

import math
import re
from typing import Any

from harborrag_adapters.connectors.attachments.processing import AttachmentMetadata
from harborrag_adapters.parsers.common.html_markdown import html_to_markdown
from harborrag_adapters.parsers.common.markdown_tables import TableCell, markdown_table
from harborrag_adapters.parsers.common.normalization import compact_text
from harborrag_core.ingestion import is_runtime_field

from .schemas import (
    JiraCustomFieldKind,
    JiraCustomFieldMetadata,
    JiraFieldContext,
)

CUSTOM_FIELD_PREFIX = "customfield_"

_ADF_TABLE_CELL_TYPES = frozenset({"tableCell", "tableHeader"})

# Markdown constructs that are only recognized at the start of a line.
_STARTS_MARKDOWN_BLOCK = re.compile(r"\||#{1,6} |> |[-*+] |\d+\. |```|~~~")

_FIELD_KEY_SEPARATORS = re.compile(r"[^a-z0-9]+")
_MAX_FILTER_VALUE_CHARS = 256

type FieldFilterValue = str | float | bool | list[str]


def build_raw_content(
    issue: dict[str, Any],
    *,
    comments: list[dict[str, Any]] | None = None,
    attachments: list[AttachmentMetadata] | None = None,
    include_attachment_text: bool = True,
) -> str:
    """Render a JIRA issue and optional child data as readable plain text."""
    fields = issue.get("fields", {})
    lines = [
        f"# {issue.get('key')} {fields.get('summary') or ''}".strip(),
        "",
        f"Type: {_name(fields.get('issuetype')) or ''}".strip(),
        f"Status: {_name(fields.get('status')) or ''}".strip(),
        f"Priority: {_name(fields.get('priority')) or ''}".strip(),
        "",
        "## Description",
        field_text(fields.get("description")),
    ]

    custom_field_lines = [
        labelled_block(field.name, field.text)
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
            lines.append(labelled_block(author, body))

    processed = [item for item in attachments or [] if item.text]
    if processed and include_attachment_text:
        lines.extend(["", "## Attachments"])
        for attachment in processed:
            # Carry the upload time into the rendered text, not just structured
            # metadata, so retrieval over an attachment's own content can still
            # answer when the file was attached.
            heading = f"### {attachment.title}"
            if attachment.created_at:
                heading = f"{heading} (attached {attachment.created_at})"
            lines.extend((heading, attachment.text or ""))

    return compact_text("\n".join(lines))


def labelled_block(label: str | None, text: str) -> str:
    """Attach a comment author or field name to the text it introduces.

    Text that opens a Markdown block starts on its own line instead: a table
    header row, heading, or list item only counts as one when it begins the
    line, so prefixing `label: ` onto it would render the block as prose and
    lose the structure the label was meant to introduce.
    """
    label = (label or "").strip()
    text = text.strip()
    if not label:
        return text
    if not text:
        return label
    separator = "\n" if _STARTS_MARKDOWN_BLOCK.match(text) else " "
    return f"{label}:{separator}{text}"


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


def field_key(name: str) -> str:
    """The payload key one custom field is filtered by: ``Skill Set`` -> ``skill_set``."""

    return _FIELD_KEY_SEPARATORS.sub("_", name.casefold()).strip("_")


def filterable_fields(
    fields: list[JiraCustomFieldMetadata],
) -> dict[str, FieldFilterValue]:
    """Map every typed custom field to one exact, filterable value.

    Prose fields are left out: they are evidence of their own, and nobody filters
    on a paragraph. A number stays a number, so a range filter works on it; an
    option or user list keeps each member, so set membership matches any one of
    them. A display name that cannot be a key -- one that normalizes to a key
    already taken, or to a runtime-only name knowledge records reject, such as
    ``Request ID`` -- falls back to the field id rather than overwriting the
    first or failing the whole document version.
    """

    output: dict[str, FieldFilterValue] = {}
    for field in fields:
        if field.value_kind == JiraCustomFieldKind.PROSE:
            continue
        value = _filter_value(field)
        if value is None:
            continue
        key = field_key(field.name)
        if not key or key in output or is_runtime_field(key):
            key = field_key(field.field_id)
        output.setdefault(key, value)
    return output


def _filter_value(field: JiraCustomFieldMetadata) -> FieldFilterValue | None:
    raw = field.value
    if field.value_kind == JiraCustomFieldKind.NUMBER:
        try:
            number = float(raw if isinstance(raw, (int, float)) else field.text)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None
    if field.value_kind == JiraCustomFieldKind.BOOLEAN:
        return raw if isinstance(raw, bool) else None
    # Read members from the raw value, not the rendered text: rendering turns an
    # email into a Markdown link and a user list into one multi-line string.
    values = list(dict.fromkeys(_member_texts(raw))) or ([field.text] if field.text else [])
    values = [value[:_MAX_FILTER_VALUE_CHARS] for value in values]
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def _member_texts(value: Any) -> list[str]:
    if isinstance(value, bool):
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, dict):
        for key in ("value", "displayName", "name", "key"):
            if isinstance(value.get(key), str) and value[key].strip():
                return [value[key].strip()]
        return []
    if isinstance(value, list):
        return [text for item in value for text in _member_texts(item)]
    return []


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
    """Extract readable text from JIRA strings, HTML, ADF, lists, or scalars.

    HTML is rendered as Markdown rather than flattened to visible text: JIRA
    descriptions, comments, and rendered custom fields routinely carry tables,
    and flattening one emits a bare cell per line, so the rows and columns that
    give each cell its meaning never reach chunk context.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        if "<" in value and ">" in value:
            return html_to_markdown(value)
        return compact_text(value)
    if isinstance(value, dict):
        adf_text = compact_text("".join(_walk_adf(value)))
        if adf_text:
            return adf_text
        for key in ("displayName", "name", "value", "key", "emailAddress"):
            if value.get(key):
                return compact_text(str(value[key]))
        nested_parts = [field_text(item) for item in value.values()]
        return compact_text("\n".join(part for part in nested_parts if part))
    if isinstance(value, list):
        return compact_text("\n".join(field_text(item) for item in value))
    return compact_text(str(value))


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
    if node_type == "table":
        # Walking a table like any other node would emit one cell per line,
        # losing the row and column each cell belongs to. Jira Cloud sends
        # every description and comment as ADF, so this is the only place a
        # Cloud issue's tables can be kept for chunk context.
        rendered = _adf_table(node)
        return ["\n", rendered, "\n"] if rendered else []

    node_parts: list[str] = []
    for child in node.get("content", []) or []:
        node_parts.extend(_walk_adf(child))
    if node_type in {"paragraph", "heading", "listItem"} and node_parts:
        node_parts.append("\n")
    return node_parts


def _adf_table(node: dict[str, Any]) -> str:
    """Render one ADF `table` node as a Markdown table."""
    rows: list[list[TableCell]] = []
    for row in node.get("content", []) or []:
        if not isinstance(row, dict) or row.get("type") != "tableRow":
            continue
        rows.append(
            [
                _adf_table_cell(cell)
                for cell in row.get("content", []) or []
                if isinstance(cell, dict) and cell.get("type") in _ADF_TABLE_CELL_TYPES
            ]
        )
    return markdown_table([row for row in rows if row])


def _adf_table_cell(cell: dict[str, Any]) -> TableCell:
    """Convert one ADF `tableCell`/`tableHeader` into a renderable cell."""
    raw_attributes = cell.get("attrs")
    attributes: dict[str, Any] = raw_attributes if isinstance(raw_attributes, dict) else {}
    return TableCell(
        text=compact_text("".join(_walk_adf(cell.get("content") or []))),
        row_span=_adf_span(attributes.get("rowspan")),
        column_span=_adf_span(attributes.get("colspan")),
        header=cell.get("type") == "tableHeader",
    )


def _adf_span(value: Any) -> int:
    """Read one ADF span attribute, which is optional and may be malformed."""
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return 1


def _name(value: Any) -> str | None:
    if not isinstance(value, dict) or not value.get("name"):
        return None
    return str(value["name"])


def _display_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    name = value.get("displayName") or value.get("name") or value.get("emailAddress")
    return str(name) if name else None
