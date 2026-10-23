"""Jira-owned canonical document transformation."""

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
    """Build Jira evidence from typed source fields instead of rendered Markdown."""

    def transform(
        self,
        raw: RawDocument,
        parsed: ParsedDocument,
        document: Document,
    ) -> Document:
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
        # in the document content. Drop them, but keep a compact per-file record
        # so the upload time stays queryable instead of being lost with them.
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
        # name. Every chunk of the issue inherits it, and the vector projection
        # stores it as ``fields.<key>``, so a search can be narrowed to, say,
        # ``fields.skill_set`` or a ``fields.years_of_experience`` range.
        extra["fields"] = filterable_fields(custom_fields)
        return replace(
            document,
            title=str(fields.get("summary") or document.title or issue_key).strip(),
            content=elements,
            # The generic normalizer derives table artifacts from the parser's
            # elements -- the rendered Markdown -- and stamps each one with the
            # `source_block_id` of the element it came from. This transform replaces
            # that element list wholesale, so every one of those artifacts would now
            # reference an element the document no longer contains, and no strategy
            # emits a table chunk for them. ProjectionVerifier requires the table
            # chunks and the canonical tables to be the same set, so carrying them
            # forward fails the document version at VerifyProjections. The grids
            # themselves are not lost: `_adf_table` and `html_to_markdown` render
            # them into the comment and custom-field text above.
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
        """Build one element per prose-bearing issue field."""

        elements: list[DocumentElement] = []
        summary = str(fields.get("summary") or "").strip()
        if summary:
            # Every chunk cites a source element ID, and the chunk validator resolves
            # those IDs against this list. An issue whose description and prose custom
            # fields are all empty would otherwise leave it empty, and its comment
            # chunks would have nothing real to cite.
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
            # The typed fields carry the issue's real substance on projects that use
            # Jira as a record store -- skills, rates, seniority, location. Keeping them
            # only as structured attributes leaves them unsearchable, so they are also
            # rendered as one evidence element, the way the connector's Markdown did.
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
