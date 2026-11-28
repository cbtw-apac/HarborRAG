"""Jira-owned canonical document transformation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from harborrag_core.domain import Document, DocumentElement, ParsedDocument, RawDocument

from .content import (
    custom_field_metadata,
    description_text,
    filterable_fields,
    labelled_block,
)
from .schemas import JiraCustomFieldKind, JiraCustomFieldMetadata

_COMMENT_CONTENT_FIELDS = frozenset({"body", "text"})
"""Comment keys holding its text, which becomes the element content, not metadata."""


class JiraDocumentTransform:
    """Build Jira evidence from typed source fields instead of rendered Markdown."""

    def transform(
        self,
        raw: RawDocument,
        parsed: ParsedDocument,
        document: Document,
    ) -> Document:
        """Replace the parsed Markdown with one element per issue field and comment.

        Raw attachment entries are dropped, because their text is already in the
        content, but a compact record per file keeps each upload time queryable.
        The typed custom fields are also kept as one flat map keyed by normalized
        display name. Every chunk inherits it and the vector projection stores it
        as ``fields.<key>``, so a search can be narrowed to ``fields.skill_set``
        or a ``fields.years_of_experience`` range.

        The generic normalizer's table artifacts are dropped. They reference the
        parsed elements this transform replaces, so no strategy would emit their
        table chunks, and ProjectionVerifier would fail the document version. The
        grids are not lost, because ADF and HTML tables render as Markdown tables
        inside the field and comment text.

        Args:
            raw: The fetched issue, with its payload in ``raw.raw``.
            parsed: The parsed rendering of the issue, which is not used.
            document: The normalized document whose provenance is kept.

        Returns:
            The issue's canonical document.
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
            description=description_text(dict(raw.raw)),
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
        extra["fields"] = filterable_fields(custom_fields)
        return replace(
            document,
            title=str(fields.get("summary") or document.title or issue_key).strip(),
            content=elements,
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
        description: str,
        issue_key: str,
        custom_fields: Sequence[JiraCustomFieldMetadata],
    ) -> list[DocumentElement]:
        """Build one element per prose-bearing issue field.

        The summary is always an element. Every chunk cites a source element ID
        from this list, so an issue whose description and prose fields are empty
        still gives its comment chunks something real to cite. The typed custom
        fields are also rendered as one evidence element, because on projects
        that use Jira as a record store they carry the issue's real substance,
        and structured attributes alone are not searchable.
        """
        elements: list[DocumentElement] = []
        summary = str(fields.get("summary") or "").strip()
        if summary:
            elements.append(
                DocumentElement(
                    id=f"jira:{issue_key}:summary",
                    type="paragraph",
                    content=f"{issue_key}: {summary}",
                    metadata={"field": "summary", "field_name": "Summary"},
                )
            )

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
        """Build one element per source comment, keyed by its provider ID."""
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
        """Render every non-prose custom field as one labelled block.

        Prose fields are already independent elements, so repeating them here would
        chunk the same text twice.
        """
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
