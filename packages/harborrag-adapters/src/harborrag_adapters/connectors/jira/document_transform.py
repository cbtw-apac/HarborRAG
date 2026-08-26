"""Jira-owned canonical document transformation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from harborrag_core.domain import Document, DocumentElement, ParsedDocument, RawDocument

from .content import custom_field_metadata, field_text
from .schemas import JiraCustomFieldKind


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

        elements: list[DocumentElement] = []
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

        custom_fields = custom_field_metadata(dict(raw.raw))
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

        extra = dict(document.provenance.extra)
        attachment_names = tuple(
            str(item.get("title") or item.get("filename") or "").strip()
            for item in self._mapping_items(raw.metadata.get("attachments"))
            if str(item.get("title") or item.get("filename") or "").strip()
        )
        extra.pop("attachments", None)
        extra["attachment_names"] = attachment_names
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
        return replace(
            document,
            title=str(fields.get("summary") or document.title or issue_key).strip(),
            content=elements,
            content_type="jira_issue",
            provenance=replace(
                document.provenance,
                record_id=issue_key,
                extra=extra,
            ),
            raw=None,
        )

    @staticmethod
    def _mapping_items(value: object) -> tuple[Mapping[str, object], ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(item for item in value if isinstance(item, Mapping))
