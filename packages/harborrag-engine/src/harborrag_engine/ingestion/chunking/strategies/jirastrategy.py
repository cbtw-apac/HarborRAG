from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from harborrag_core.contracts.chunking import SplitBoundaryKind, TokenCounter
from harborrag_core.domain.element import DocumentElement

from ..config import ChunkingProfile
from ..schemas import ChunkingRequest, ChunkUnit
from ..segmentation.base import element_span

_ROLE_ALIASES = {
    "summary": "jira.summary",
    "metadata": "jira.summary",
    "description": "jira.description",
    "custom_fields": "jira.summary",
    "acceptance": "jira.acceptance_criteria",
    "acceptance_criteria": "jira.acceptance_criteria",
    "environment": "jira.environment",
    "comment": "jira.comment",
    "comments": "jira.comment",
    "changelog": "jira.changelog",
    "attachment": "jira.attachment",
    "attachments": "jira.attachment",
}

_SECTION_FIELDS = {
    "description": "description",
    "custom_fields": "custom_fields",
    "comments": "comment",
    "attachments": "attachment",
}

_CHILD_COLLECTIONS = frozenset({"attachments", "comments", "changelog", "custom_fields"})
_ENTITY_CONTENT_FIELDS = frozenset({"body", "download_url", "items", "reason", "text"})
_OVERVIEW_FIELDS = frozenset({"summary", "metadata", "custom_fields"})


class JiraChunkingStrategy:
    """Create independent units for Jira fields, comments, and attachments."""

    name = "jira"
    version = "3"

    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    def create_units(
        self,
        request: ChunkingRequest,
        profile: ChunkingProfile,
    ) -> tuple[ChunkUnit, ...]:
        """Create source-ordered units from normalized Jira elements."""

        del profile
        provenance = dict(request.document.provenance.extra)
        issue_key = provenance.get("issue_key") or request.document.provenance.record_id
        common_metadata = {
            key: value for key, value in provenance.items() if key not in _CHILD_COLLECTIONS
        }
        elements = self._field_elements(request, provenance)
        entity_source_ids = self._entity_source_ids(request.document.content)
        units = [
            unit
            for ordinal, (element, field) in enumerate(elements)
            if (
                unit := self._unit(
                    request=request,
                    element=element,
                    field=field,
                    source_ordinal=ordinal,
                    issue_key=issue_key,
                    common_metadata=common_metadata,
                )
            )
            is not None
        ]

        source_ordinal = len(elements)
        for collection, field in (
            ("comments", "comment"),
            ("attachments", "attachment"),
            ("changelog", "changelog"),
        ):
            source_position = 0
            for item in self._mapping_items(provenance.get(collection)):
                content = self._entity_content(field, item)
                if not content.strip():
                    continue
                metadata = {
                    key: value for key, value in item.items() if key not in _ENTITY_CONTENT_FIELDS
                }
                metadata["field"] = field
                source_ids = entity_source_ids.get(field) or ()
                source_id = (
                    source_ids[min(source_position, len(source_ids) - 1)]
                    if source_ids
                    else request.document.content[-1].id
                )
                source_position += 1
                element = DocumentElement(
                    id=source_id,
                    type="paragraph",
                    content=content,
                    metadata=metadata,
                )
                unit = self._unit(
                    request=request,
                    element=element,
                    field=field,
                    source_ordinal=source_ordinal,
                    issue_key=issue_key,
                    common_metadata=common_metadata,
                )
                source_ordinal += 1
                if unit is not None:
                    units.append(unit)
        return tuple(units)

    def _field_elements(
        self,
        request: ChunkingRequest,
        provenance: Mapping[str, Any],
    ) -> list[tuple[DocumentElement, str]]:
        elements = request.document.content
        if not self._uses_connector_layout(elements):
            return [
                (element, self._explicit_field(element) or "description") for element in elements
            ]

        output: list[tuple[DocumentElement, str]] = []
        current_field = "metadata"
        summary_added = False
        issue_key = str(
            provenance.get("issue_key") or request.document.provenance.record_id
        ).strip()
        for element in elements:
            explicit_field = self._explicit_field(element)
            if explicit_field is not None:
                output.append((element, explicit_field))
                continue

            if element.type == "heading":
                level = element.metadata.get("level")
                heading = self._normalize_field(element.content or "")
                if level == 1 and not summary_added:
                    summary = request.document.title
                    if issue_key and not summary.startswith(issue_key):
                        summary = f"{issue_key}: {summary}"
                    output.append(
                        (
                            DocumentElement(
                                id=element.id,
                                type="heading",
                                content=summary,
                                metadata={**element.metadata, "field": "summary"},
                            ),
                            "summary",
                        )
                    )
                    summary_added = True
                elif level == 2 and heading in _SECTION_FIELDS:
                    current_field = _SECTION_FIELDS[heading]
                # Connector-owned headings label sections; they are context, not body text.
                continue

            # Child entities are reconstructed from connector metadata so IDs and
            # attachment boundaries survive Markdown parsing.
            collection = f"{current_field}s"
            if current_field in {"comment", "attachment"} and self._mapping_items(
                provenance.get(collection)
            ):
                continue
            output.append((element, current_field))

        custom_field_target = (
            "description" if any(field == "description" for _, field in output) else "summary"
        )
        return [
            (
                DocumentElement(
                    id=element.id,
                    type=element.type,
                    content=element.content,
                    metadata={**element.metadata, "source_field": "custom_fields"},
                ),
                custom_field_target,
            )
            if field == "custom_fields"
            else (element, field)
            for element, field in output
        ]

    def _unit(
        self,
        *,
        request: ChunkingRequest,
        element: DocumentElement,
        field: str,
        source_ordinal: int,
        issue_key: object,
        common_metadata: Mapping[str, Any],
    ) -> ChunkUnit | None:
        content = element.content or ""
        if not content.strip():
            return None
        normalized_field = self._normalize_field(field)
        role = _ROLE_ALIASES.get(normalized_field, f"jira.{normalized_field}")
        metadata = {
            **common_metadata,
            **element.metadata,
            "issue_key": issue_key,
            "field": normalized_field,
            "issue_summary": request.document.title,
            "source_ordinal": source_ordinal,
        }
        if role == "jira.comment":
            metadata["comment_id"] = metadata.get("comment_id") or metadata.get("id") or element.id
        elif role == "jira.changelog":
            metadata["change_id"] = metadata.get("change_id") or metadata.get("id") or element.id
        elif role == "jira.attachment":
            metadata["attachment_id"] = (
                metadata.get("attachment_id") or metadata.get("id") or element.id
            )
        anchor, merge_group = self._identity(role, metadata, element.id)
        independent = role in {"jira.comment", "jira.changelog", "jira.attachment"}
        count = self._token_counter.count(content)
        if count < 1:
            return None
        path_label = (
            "overview"
            if normalized_field in _OVERVIEW_FIELDS
            else str(metadata.get("title") or normalized_field).strip() or normalized_field
        )
        return ChunkUnit(
            anchor=anchor,
            content=content,
            token_count=count,
            role=role,
            structural_path=(request.document.title, path_label),
            source_span=element_span(element.id, content, element.metadata),
            merge_group=merge_group,
            boundary_kind=SplitBoundaryKind.PARAGRAPH,
            hard_boundary_before=independent,
            hard_boundary_after=independent,
            metadata=metadata,
        )

    @staticmethod
    def _uses_connector_layout(elements: Sequence[DocumentElement]) -> bool:
        return any(
            element.type == "heading"
            and element.metadata.get("level") == 2
            and JiraChunkingStrategy._normalize_field(element.content or "") in _SECTION_FIELDS
            for element in elements
        )

    @staticmethod
    def _entity_source_ids(
        elements: Sequence[DocumentElement],
    ) -> dict[str, tuple[str, ...]]:
        values: dict[str, list[str]] = {"comment": [], "attachment": [], "changelog": []}
        current_field = ""
        for element in elements:
            if element.type == "heading":
                level = element.metadata.get("level")
                heading = JiraChunkingStrategy._normalize_field(element.content or "")
                if level == 2 and heading in _SECTION_FIELDS:
                    current_field = _SECTION_FIELDS[heading]
                elif current_field == "attachment" and level == 3:
                    values["attachment"].append(element.id)
                continue
            if current_field == "comment":
                values["comment"].append(element.id)
        fallback = elements[-1].id if elements else "jira-source"
        values["changelog"].append(fallback)
        return {key: tuple(items) for key, items in values.items()}

    @staticmethod
    def _explicit_field(element: DocumentElement) -> str | None:
        value = (
            element.metadata.get("field")
            or element.metadata.get("field_name")
            or element.metadata.get("role")
        )
        return JiraChunkingStrategy._normalize_field(str(value)) if value else None

    @staticmethod
    def _normalize_field(value: str) -> str:
        return "_".join(value.strip().lower().replace("-", " ").split())

    @staticmethod
    def _mapping_items(value: object) -> tuple[Mapping[str, Any], ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return ()
        return tuple(item for item in value if isinstance(item, Mapping))

    @staticmethod
    def _entity_content(field: str, item: Mapping[str, Any]) -> str:
        if field == "comment":
            return str(item.get("body") or "")
        if field == "attachment":
            return str(item.get("text") or "")
        lines = []
        for change in JiraChunkingStrategy._mapping_items(item.get("items")):
            name = change.get("field") or "field"
            before = change.get("from_value") or change.get("from") or ""
            after = change.get("to_value") or change.get("to") or ""
            lines.append(f"{name}: {before} -> {after}")
        return "\n".join(lines)

    @staticmethod
    def _identity(
        role: str,
        metadata: dict[str, object],
        element_id: str,
    ) -> tuple[str, str]:
        if role == "jira.comment":
            comment_id = metadata.get("comment_id") or metadata.get("id") or element_id
            return f"comment:{comment_id}", f"comment:{comment_id}"
        if role == "jira.changelog":
            event_id = metadata.get("change_id") or metadata.get("id") or element_id
            return f"changelog:{event_id}", f"changelog:{event_id}"
        if role == "jira.attachment":
            attachment_id = metadata.get("attachment_id") or metadata.get("id") or element_id
            return f"attachment:{attachment_id}", f"attachment:{attachment_id}"
        field = role.removeprefix("jira.")
        return f"field:{field}", f"issue-{field}"
