"""Text extraction and rendering for JIRA issue payloads."""

from __future__ import annotations

from typing import Any

from harborrag_adapters.connectors.attachments.processing import AttachmentMetadata
from harborrag_adapters.parsers.text_extraction import compact_text, html_to_text

from .schemas import JiraCustomFieldMetadata

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

    values: list[JiraCustomFieldMetadata] = []
    for field_id, value in fields.items():
        if not str(field_id).startswith(CUSTOM_FIELD_PREFIX):
            continue
        schema = schemas.get(field_id) or {}
        rendered_value = rendered_fields.get(field_id)
        text_source = rendered_value if rendered_value not in (None, "") else value
        values.append(
            JiraCustomFieldMetadata(
                field_id=str(field_id),
                name=str(names.get(field_id) or field_id),
                schema_type=schema.get("type") if isinstance(schema, dict) else None,
                custom_type=schema.get("custom") if isinstance(schema, dict) else None,
                value=value,
                text=field_text(text_source),
            )
        )
    return values


def field_text(value: Any) -> str:
    """Extract readable text from JIRA strings, HTML, ADF, lists, or scalars."""
    if value is None:
        return ""
    if isinstance(value, str):
        if "<" in value and ">" in value:
            return html_to_text(value)
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

    node_parts: list[str] = []
    for child in node.get("content", []) or []:
        node_parts.extend(_walk_adf(child))
    if node_type in {"paragraph", "heading", "listItem"} and node_parts:
        node_parts.append("\n")
    return node_parts


def _name(value: Any) -> str | None:
    if not isinstance(value, dict) or not value.get("name"):
        return None
    return str(value["name"])


def _display_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    name = value.get("displayName") or value.get("name") or value.get("emailAddress")
    return str(name) if name else None
