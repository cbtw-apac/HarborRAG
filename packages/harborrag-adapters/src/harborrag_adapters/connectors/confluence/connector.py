"""Confluence discovery and raw-document loading orchestration."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from itertools import islice
from typing import Any

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.schemas import ConnectorCapabilities, ConnectorQuery
from harborrag_adapters.connectors.shared.attachments import AttachmentProcessor
from harborrag_adapters.parsers import HarborParser
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .client import ConfluenceClient, _RequestsConfluenceClient
from .config import ConfluenceSpaceConfig
from .content import ConfluenceContentAPI
from .mappers import (
    body_html_from_content,
    build_document_metadata,
    build_source_record,
    content_id_from_record,
    display_url,
)
from .utils import build_cql, validate_content_id

logger = logging.getLogger("harborrag.adapters.connectors.confluence")


class ConfluenceConnector(BaseConnector):
    """Connector for Confluence Cloud and Data Center REST APIs.

    Discovery returns page/blogpost source records from CQL search. Loading a
    record fetches the expanded content body and optional comments/attachments so
    downstream parsing receives one complete page document.
    """

    provider_name = "confluence"
    capabilities = ConnectorCapabilities(
        pagination=True,
        attachments=True,
        comments=True,
        labels=True,
        incremental_sync=True,
        full_sync=True,
        relationships=True,
    )

    def __init__(
        self,
        config: ConfluenceSpaceConfig,
        *,
        client: ConfluenceClient | None = None,
        parser: HarborParser | None = None,
    ) -> None:
        """Initialize provider APIs and shared attachment processing."""
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.client = client or _RequestsConfluenceClient(config)
        self._content = ConfluenceContentAPI(self.client, config)
        self._attachments = AttachmentProcessor(
            download_fn=self.client.download_bytes,
            base_url=self.base_url,
            parser=parser,
            custom_parsers=config.custom_parsers,
            process_attachment_callback=config.process_attachment_callback,
            max_attachment_size_bytes=config.max_attachment_size_bytes,
            fail_on_error=config.fail_on_error,
            logger_=logger,
        )

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Search Confluence content or materialize explicitly requested IDs."""
        query = query or ConnectorQuery()
        content_ids = self._content_ids_from_query(query)
        if content_ids:
            ids: Iterator[str] = self._content.with_children(content_ids, query)
            if query.limit is not None:
                ids = islice(ids, query.limit)
            for content_id in ids:
                yield self._record_for_id(content_id, query)
            return

        cql = self._cql_from_query(query)
        yielded = 0
        for content in self._content.search(cql):
            content_id = str(content.get("id") or "<unknown>")
            self._validate_content(content, content_id)
            if not self._should_process_content(content):
                continue
            record = build_source_record(
                content,
                base_url=self.base_url,
                deployment_type=self.config.deployment,
                default_space_key=self.config.space_key,
            )
            record.metadata["include_attachments"] = query.include_attachments
            yield record
            yielded += 1
            if query.limit is not None and yielded >= query.limit:
                return

    def load(self, record: SourceRecord) -> RawDocument:
        """Load one expanded Confluence content item as an HTML raw document."""
        content_id = content_id_from_record(record)
        content = self._content.get_content(content_id)
        self._validate_content(content, content_id)

        if not self._should_process_content(content):
            raise DocumentProcessingError(
                f"Confluence content {content_id} does not match label filters"
            )
        comments = self._content.fetch_comments(content_id) if self.config.include_comments else []
        attachments = []
        include_attachments = bool(
            self.config.include_attachments and record.metadata.get("include_attachments", True)
        )
        if include_attachments:
            attachments = self._attachments.process(self._content.list_attachments(content_id))

        metadata = build_document_metadata(
            content,
            comments=comments,
            attachments=attachments,
        )
        body_html = body_html_from_content(content)
        source_url = display_url(
            self.base_url,
            self.config.deployment,
            metadata.space_key,
            metadata.content_id,
            metadata.title,
        )

        return RawDocument(
            id=record.id,
            source=source_url,
            content=body_html,
            content_type="text/html",
            metadata=metadata.to_dict(),
            raw=content,
        )

    def load_by_ids(self, content_ids: list[str]) -> Iterator[RawDocument]:
        """Load content for callers that already have Confluence IDs."""
        for content_id in content_ids:
            yield self.load(self._record_for_id(content_id, ConnectorQuery()))

    def _cql_from_query(self, query: ConnectorQuery) -> str:
        """Translate shared connector filters into Confluence CQL."""
        filters = query.filters
        space_key = str(filters.get("space_key") or query.path or self.config.space_key)
        if space_key != self.config.space_key:
            raise ValueError(
                f"Confluence query space {space_key!r} is outside configured "
                f"space {self.config.space_key!r}"
            )
        return build_cql(
            space_key=space_key,
            content_types=self._list_filter(
                filters.get("content_types"),
                default=self.config.content_types,
            ),
            labels=self._list_filter(
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
    def _list_filter(value: Any, *, default: list[str]) -> list[str]:
        if value is None:
            return list(default)
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]

    def _record_for_id(self, content_id: str, query: ConnectorQuery) -> SourceRecord:
        """Build a direct-load record when discovery is driven by explicit IDs."""
        content = self._content.get_content_summary(validate_content_id(content_id))
        self._validate_content(content, content_id)
        record = build_source_record(
            content,
            base_url=self.base_url,
            deployment_type=self.config.deployment,
            default_space_key=self.config.space_key,
        )
        record.metadata["include_attachments"] = query.include_attachments
        return record

    def _should_process_content(self, content: dict[str, Any]) -> bool:
        """Apply include/exclude label filters to Confluence content."""
        labels = content.get("metadata", {}).get("labels", {}).get("results", [])
        label_names = {str(label.get("name")) for label in labels if isinstance(label, dict)}
        if self.config.exclude_labels and label_names.intersection(self.config.exclude_labels):
            return False
        if self.config.include_labels:
            return bool(label_names.intersection(self.config.include_labels))
        return True

    def _validate_content(self, content: dict[str, Any], content_id: str) -> None:
        """Fail fast when content is malformed or outside the configured space."""
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
