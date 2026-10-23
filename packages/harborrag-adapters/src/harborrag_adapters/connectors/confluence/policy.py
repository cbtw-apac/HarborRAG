"""Confluence query translation and content filtering."""

from __future__ import annotations

from typing import Any

from harborrag_adapters.connectors.attachments import attachment_ids_from_filters
from harborrag_adapters.connectors.query_values import normalized_string_list
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord

from .config import ConfluenceSpaceConfig
from .content import ConfluenceContentAPI
from .discovery import DISCOVERY_DESCRIPTOR_KEY
from .mappers import build_source_record, validate_content
from .query import build_cql, validate_content_id


class ConfluenceQueryPolicyMixin:
    """Mixin translating queries to CQL and enforcing the configured space and content filters.

    Expects the host to provide ``config``, ``base_url`` and ``_content``; the annotations declare
    that contract and create no attributes.
    """

    config: ConfluenceSpaceConfig
    base_url: str
    _content: ConfluenceContentAPI

    def _cql_from_query(self, query: ConnectorQuery) -> str:
        """Translate a query into CQL, rejecting any space other than the configured one."""
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
            # TODO: validate the ``content_types`` filter against the page/blogpost allowlist.
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
        """Return validated IDs from the ``content_ids`` or ``page_ids`` filter."""
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
        """Copy per-query load options (attachments, comments, graph, selection) onto the record."""
        record.metadata["include_attachments"] = query.include_attachments
        record.metadata["include_comments"] = bool(query.filters.get("include_comments", True))
        record.metadata["build_graph"] = bool(query.filters.get("build_graph", True))
        selected = attachment_ids_from_filters(query.filters)
        if selected:
            record.metadata["_selected_attachment_ids"] = selected
        return record

    def _should_process_content(self, content: dict[str, Any]) -> bool:
        """Apply include/exclude label filters and reject live docs unconditionally."""
        if content.get("subtype") == "live":
            return False
        labels = content.get("metadata", {}).get("labels", {}).get("results", [])
        label_names = {str(label.get("name")) for label in labels if isinstance(label, dict)}
        if self.config.exclude_labels and label_names.intersection(self.config.exclude_labels):
            return False
        if self.config.include_labels:
            return bool(label_names.intersection(self.config.include_labels))
        return True

    def _validate_content(self, content: dict[str, Any], content_id: str) -> None:
        """Fail fast when content is malformed or outside the configured space."""
        validate_content(content, content_id, space_key=self.config.space_key)

    def _record_for_id(self, content_id: str, query: ConnectorQuery) -> SourceRecord:
        """Build a direct-load record for an explicit content ID."""
        content = self._content.get_content_summary(validate_content_id(content_id))
        self._validate_content(content, content_id)
        record = build_source_record(
            content,
            base_url=self.base_url,
            deployment_type=self.config.deployment,
            default_space_key=self.config.space_key,
        )
        record.metadata[DISCOVERY_DESCRIPTOR_KEY] = content
        return self._apply_query_policy(record, query)
