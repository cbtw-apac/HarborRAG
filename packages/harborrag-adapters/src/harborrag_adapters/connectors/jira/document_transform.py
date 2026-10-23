"""Jira canonical document transformation.

Handles:
- Evidence elements from summary, description, custom fields and comments
- Provenance with attachment details and typed custom attributes
- A flat ``fields`` filter map for retrieval narrowing

Does NOT parse Markdown; the issue payload on the raw document is the source.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from harborrag_core.domain import Document, DocumentElement, ParsedDocument, RawDocument

from .content import custom_field_metadata, field_text, filterable_fields, labelled_block
from .schemas import JiraCustomFieldKind, JiraCustomFieldMetadata

# The comment's own text becomes the element content, so it must not be repeated
# as metadata a projection would then carry twice.
_COMMENT_CONTENT_FIELDS = frozenset({"body", "text"})


class JiraDocumentTransform:
    """Transform building Jira evidence from typed source fields instead of Markdown."""

    def transform(
        self,
        raw: RawDocument,
        parsed: ParsedDocument,
        document: Document,
    ) -> Document:
        """Replace the parsed elements with elements built from the issue payload.

        Provenance gains attachment details, custom fields and a flat ``fields`` filter
        map; ``table_artifacts`` are cleared.

        Args:
            raw: Raw document whose ``raw`` is the issue payload.
            parsed: Parsed document; unused.
            document: Canonical document to rebuild.

        Returns:
            Document of type ``jira_issue`` with summary, description, prose-field,
            typed-field and comment elements.

        Raises:
            ValueError: If the issue payload, its fields or its key is missing.
        """

        del parsed
        if not isinstance(raw.raw, Mapping):
            raise ValueError("Jira canonical normalization requires the source issue payload")
        fields = raw.raw.get("fields")
        if not isinstance(fields, Mapping):
            raise ValueError("Jira source issue payload has no fields object")
        issue_key = str(raw.raw.get("key") or raw.metadata.get("issue_key") or "").strip()
        if not issue_key:
            raise ValueError("Jira source issue payload has no issue key")

        custom_fields = custom_field_metadata(dict(raw.raw))
        elements = self._field_elements(
            fields=fields,
            issue_key=issue_key,
            custom_fields=custom_fields,
        )
        elements.extend(
            self._comment_elements(
                comments=raw.metadata.get("comments"),
                issue_key=issue_key,
            )
        )

        extra = dict(document.provenance.extra)
        attachment_items = self._mapping_items(raw.metadata.get("attachments"))
        attachment_names = tuple(
            str(item.get("title") or item.get("filename") or "").strip()
            for item in attachment_items
            if str(item.get("title") or item.get("filename") or "").strip()
        )
        # The raw entries also carry extracted attachment text, which is already
        # in the document content.
        extra.pop("attachments", None)
        extra["attachment_names"] = attachment_names
        extra["attachment_details"] = [
            {
                "title": title,
                "created_at": item.get("created_at"),
                "media_type": item.get("media_type"),
                "size_bytes": item.get("size_bytes"),
                "status": item.get("status"),
            }
            for item in attachment_items
            if (title := str(item.get("title") or item.get("filename") or "").strip())
        ]
        extra["custom_fields"] = [
            {
                "field_id": field.field_id,
                "name": field.name,
                "schema_type": field.schema_type,
                "custom_type": field.custom_type,
                "value": field.value,
                "text": field.text,
                "value_kind": field.value_kind.value,
                "context": {
                    "project_id": field.context.project_id,
                    "project_key": field.context.project_key,
                    "issue_type_id": field.context.issue_type_id,
                    "issue_type_name": field.context.issue_type_name,
                },
            }
            for field in custom_fields
        ]
        extra["typed_custom_attributes"] = [
            item
            for item in extra["custom_fields"]
            if item["value_kind"] != JiraCustomFieldKind.PROSE.value
        ]
        # The same typed fields as one flat, exact map keyed by normalized display
        # name. Every chunk of the issue inherits it.
        extra["fields"] = filterable_fields(custom_fields)
        return replace(
            document,
            title=str(fields.get("summary") or document.title or issue_key).strip(),
            content=elements,
            # The parser's table artifacts cite elements this transform replaces; carrying them
            # forward fails projection verification.
            table_artifacts=(),
            content_type="jira_issue",
            provenance=replace(
                document.provenance,
                record_id=issue_key,
                extra=extra,
            ),
            raw=None,
        )

    def _field_elements(
        self,
        *,
        fields: Mapping[str, Any],
        issue_key: str,
        custom_fields: Sequence[JiraCustomFieldMetadata],
    ) -> list[DocumentElement]:
        """Build elements for the summary, description, prose fields and typed-field block."""

        elements: list[DocumentElement] = []
        summary = str(fields.get("summary") or "").strip()
        if summary:
            # Without the summary element, an issue with empty description and prose fields
            # would have no element for comment chunks to cite.
            elements.append(
                DocumentElement(
                    id=f"jira:{issue_key}:summary",
                    type="paragraph",
                    content=f"{issue_key}: {summary}",
                    metadata={"field": "summary", "field_name": "Summary"},
                )
            )

        description = field_text(fields.get("description"))
        if description:
            elements.append(
                DocumentElement(
                    id=f"jira:{issue_key}:description",
                    type="paragraph",
                    content=description,
                    metadata={"field": "description", "field_name": "Description"},
                )
            )

        for field in custom_fields:
            if not field.is_searchable_prose or not field.text:
                continue
            elements.append(
                DocumentElement(
                    id=f"jira:{issue_key}:{field.field_id}",
                    type="paragraph",
                    content=field.text,
                    metadata={
                        "field": "jira_field",
                        "field_id": field.field_id,
                        "field_name": field.name,
                        "value_kind": field.value_kind.value,
                        "project_id": field.context.project_id,
                        "project_key": field.context.project_key,
                        "issue_type_id": field.context.issue_type_id,
                        "issue_type_name": field.context.issue_type_name,
                    },
                )
            )

        typed_block = self._typed_custom_field_block(custom_fields)
        if typed_block:
            # Keeping them only as structured attributes leaves them unsearchable, so they are
            # also rendered as one evidence element, the way the connector's Markdown did.
            elements.append(
                DocumentElement(
                    id=f"jira:{issue_key}:custom_fields",
                    type="paragraph",
                    content=typed_block,
                    metadata={"field": "custom_fields", "field_name": "Custom Fields"},
                )
            )
        return elements

    def _comment_elements(
        self,
        *,
        comments: object,
        issue_key: str,
    ) -> list[DocumentElement]:
        """Build one element per non-empty comment, keyed by its provider id."""

        elements: list[DocumentElement] = []
        for ordinal, comment in enumerate(self._mapping_items(comments)):
            body = str(comment.get("body") or comment.get("text") or "").strip()
            if not body:
                continue
            comment_id = str(comment.get("comment_id") or comment.get("id") or ordinal).strip()
            elements.append(
                DocumentElement(
                    id=f"jira:{issue_key}:comment:{comment_id}",
                    type="paragraph",
                    content=body,
                    metadata={
                        **{
                            key: value
                            for key, value in comment.items()
                            if key not in _COMMENT_CONTENT_FIELDS
                        },
                        "field": "comment",
                        "field_name": "Comment",
                        "comment_id": comment_id,
                    },
                )
            )
        return elements

    @staticmethod
    def _typed_custom_field_block(
        custom_fields: Sequence[JiraCustomFieldMetadata],
    ) -> str:
        """Render the non-prose custom fields as one labelled block."""

        return "\n".join(
            labelled_block(field.name, field.text)
            for field in custom_fields
            if not field.is_searchable_prose and field.text
        )

    @staticmethod
    def _mapping_items(value: object) -> tuple[Mapping[str, object], ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(item for item in value if isinstance(item, Mapping))
