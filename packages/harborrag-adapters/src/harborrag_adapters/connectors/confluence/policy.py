"""Confluence query translation and content-policy helpers."""

from __future__ import annotations

from typing import Any

from harborrag_adapters.connectors.attachments import attachment_ids_from_filters
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.query_values import normalized_string_list
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord

from .config import ConfluenceSpaceConfig
from .query import build_cql, validate_content_id


class ConfluenceQueryPolicyMixin:
    """Translate queries and enforce the configured Confluence scope.

    The eleven `self.config` reads below are the mixin's contract with its host. Declaring
    it here is annotation-only — it creates no attribute at runtime — but it makes the
    dependency explicit and type-checked instead of one the host merely happens to satisfy.
    """

    config: ConfluenceSpaceConfig

    def _cql_from_query(self, query: ConnectorQuery) -> str:
        filters = query.filters
        requested_spaces = normalized_string_list(filters.get("space_keys"), default=[])
        if requested_spaces and set(requested_spaces) != {self.config.space_key}:
            raise ValueError(
                "Confluence query spaces must stay inside the configured connection space"
            )
        space_key = str(filters.get("space_key") or query.path or self.config.space_key)
        if space_key != self.config.space_key:
            raise ValueError(
                f"Confluence query space {space_key!r} is outside configured "
                f"space {self.config.space_key!r}"
            )
        return build_cql(
            space_key=space_key,
            content_types=normalized_string_list(
                filters.get("content_types"),
                default=self.config.content_types,
            ),
            labels=normalized_string_list(
                filters.get("labels") or filters.get("label"),
                default=self.config.include_labels,
            ),
            updated_after=query.updated_after,
            text_search=query.pattern,
            raw_cql=filters.get("cql"),
        )

    @staticmethod
    def _content_ids_from_query(query: ConnectorQuery) -> list[str]:
        values = query.filters.get("content_ids") or query.filters.get("page_ids")
        if values is None:
            return []
        if isinstance(values, str):
            return [validate_content_id(values)]
        return [validate_content_id(str(value)) for value in values]

    @staticmethod
    def _apply_query_policy(
        record: SourceRecord,
        query: ConnectorQuery,
    ) -> SourceRecord:
        record.metadata["include_attachments"] = query.include_attachments
        record.metadata["include_comments"] = bool(query.filters.get("include_comments", True))
        record.metadata["build_graph"] = bool(query.filters.get("build_graph", True))
        selected = attachment_ids_from_filters(query.filters)
        if selected:
            record.metadata["_selected_attachment_ids"] = selected
        return record

    def _should_process_content(self, content: dict[str, Any]) -> bool:
        labels = content.get("metadata", {}).get("labels", {}).get("results", [])
        label_names = {str(label.get("name")) for label in labels if isinstance(label, dict)}
        if self.config.exclude_labels and label_names.intersection(self.config.exclude_labels):
            return False
        if self.config.include_labels:
            return bool(label_names.intersection(self.config.include_labels))
        return True

    def _validate_content(self, content: dict[str, Any], content_id: str) -> None:
        space_key = content.get("space", {}).get("key")
        missing = [
            name
            for name, value in (
                ("id", content.get("id")),
                ("title", content.get("title")),
                ("space.key", space_key),
            )
            if not value
        ]
        if missing:
            raise DocumentProcessingError(
                f"Confluence content {content_id} missing required fields: {', '.join(missing)}"
            )
        if str(space_key) != self.config.space_key:
            raise DocumentProcessingError(
                f"Confluence content {content_id} belongs to space {space_key!r}, "
                f"outside configured space {self.config.space_key!r}"
            )
