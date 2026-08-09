"""Text extraction and rendering for JIRA issue payloads."""

from __future__ import annotations

from typing import Any

from harborrag_adapters.connectors.attachments.processing import AttachmentMetadata
from harborrag_adapters.parsers.common.normalization import compact_text, html_to_text

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
