"""Confluence discovery and raw-document loading orchestration."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from itertools import islice

from harborrag_adapters.connectors.attachments import (
    AttachmentDocumentLoader,
    AttachmentProcessor,
    AttachmentSourceGateway,
    AttachmentSourcePolicy,
    is_attachment_record,
)
from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.descriptors import ConnectorDocumentDescriptor
from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    DocumentProcessingError,
    FetchError,
)
from harborrag_adapters.connectors.policies.http import summarize_provider_error
from harborrag_adapters.connectors.rate_limiting import ConnectorRateLimiter
from harborrag_adapters.connectors.schemas import (
    ConnectorCapabilities,
    ConnectorPage,
    ConnectorQuery,
)
from harborrag_adapters.parsers import HarborParserRegistry
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .client import ConfluenceClient, _RequestsConfluenceClient
from .config import ConfluenceSpaceConfig
from .content import ConfluenceContentAPI
from .discovery import DISCOVERY_DESCRIPTOR_KEY, ConfluenceDescriptorBuilder
from .mappers import (
    body_html_from_content,
    build_document_metadata,
    build_source_record,
    content_id_from_record,
    display_url,
)
from .policy import ConfluenceQueryPolicyMixin
from .query import validate_content_id
from .relations import ConfluenceSourceRelationResolver

logger = logging.getLogger("harborrag.adapters.connectors.confluence")


class ConfluenceConnector(ConfluenceQueryPolicyMixin, BaseConnector):
    """Connector for Confluence Cloud and Data Center REST APIs.

    Discovery returns page/blogpost records from CQL search. Loading fetches the expanded
    body and optional comments/attachments so parsing receives one complete document.
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
        concurrent_describe=True,
    )

    def __init__(
        self,
        config: ConfluenceSpaceConfig,
        *,
        client: ConfluenceClient | None = None,
        parser: HarborParserRegistry | None = None,
        rate_limiter: ConnectorRateLimiter | None = None,
    ) -> None:
        """Initialize provider APIs and shared attachment processing."""
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.client = client or _RequestsConfluenceClient(
            config,
            rate_limiter=rate_limiter,
        )
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
        attachment_sources = AttachmentSourceGateway(
            download_fn=self.client.download_bytes,
            policy=AttachmentSourcePolicy(
                base_url=self.base_url,
                process_callback=config.process_attachment_callback,
                max_size_bytes=config.max_attachment_size_bytes,
                fail_on_error=config.fail_on_error,
            ),
            logger_=logger,
        )
        self._attachment_loader = AttachmentDocumentLoader(attachment_sources)
        self._descriptors = ConfluenceDescriptorBuilder(
            content=self._content,
            attachments=attachment_sources,
            config=config,
            base_url=self.base_url,
        )
        self._relations = ConfluenceSourceRelationResolver()

    def close(self) -> None:
        """Release the client session when the connector owns one."""

        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def connect(self) -> None:
        """Verify Confluence credentials once before the first discovery request.

        Both branches re-raise through the same message template so a bad
        credential looks the same shape on Confluence and Jira: the 401 case
        otherwise reaches the caller as a bare, un-prefixed provider message
        (raised deep in the shared HTTP client, bypassing this method's own
        framing), while only 403 used to get labeled as this connector's.
        """
        try:
            self.client.get_json("user/current")
        except AuthenticationError as exc:
            raise AuthenticationError(
                f"Confluence authentication failed: {summarize_provider_error(exc)}"
            ) from exc
        except FetchError as exc:
            if exc.status_code == 403:
                raise AuthenticationError(
                    f"Confluence authentication failed: {summarize_provider_error(exc)}"
                ) from exc
            raise

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Search Confluence content or materialize explicitly requested IDs."""
        self._ensure_connected()
        query = query or ConnectorQuery()
        content_ids = self._content_ids_from_query(query)
        yielded = 0
        mode = "explicit" if content_ids else "search"
        logger.info(
            "Confluence discovery started mode=%s limit=%s recursive=%s",
            mode,
            query.limit,
            query.recursive,
        )
        try:
            if content_ids:
                ids: Iterator[str] = self._content.with_children(content_ids, query)
                if query.limit is not None:
                    ids = islice(ids, query.limit)
                for content_id in ids:
                    record = self._record_for_id(content_id, query)
                    yielded += 1
                    yield record
                return

            cql = self._cql_from_query(query)
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
                record.metadata[DISCOVERY_DESCRIPTOR_KEY] = content
                record = self._apply_query_policy(record, query)
                yielded += 1
                yield record
                if query.limit is not None and yielded >= query.limit:
                    return
        finally:
            logger.info(
                "Confluence discovery iterator closed mode=%s yielded=%d",
                mode,
                yielded,
            )

    def discover_page(
        self,
        query: ConnectorQuery | None,
        *,
        cursor: str | None,
        page_size: int,
    ) -> ConnectorPage:
        """Use Confluence's Cloud cursor or Data Center offset directly."""

        self._ensure_connected()
        query = query or ConnectorQuery()
        if self._content_ids_from_query(query):
            page = super().discover_page(query, cursor=cursor, page_size=page_size)
            logger.debug(
                "Confluence discovery page mode=explicit records=%d has_next=%s",
                len(page.records),
                page.next_cursor is not None,
            )
            return page
        cql = self._cql_from_query(query)
        output: list[SourceRecord] = []
        next_cursor = cursor
        while len(output) < page_size:
            content_page, next_cursor = self._content.search_page(
                cql,
                cursor=next_cursor,
                limit=page_size - len(output),
            )
            for content in content_page:
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
                record.metadata[DISCOVERY_DESCRIPTOR_KEY] = content
                output.append(self._apply_query_policy(record, query))
            if next_cursor is None:
                break
        page = ConnectorPage(tuple(output), next_cursor)
        logger.debug(
            "Confluence discovery page mode=search records=%d has_next=%s",
            len(page.records),
            page.next_cursor is not None,
        )
        return page

    def load(self, record: SourceRecord) -> RawDocument:
        """Load one expanded Confluence content item as an HTML raw document."""

        if is_attachment_record(record):
            if (
                not self.config.include_attachments
                or record.metadata.get("include_attachments") is False
            ):
                raise DocumentProcessingError("Confluence attachment loading is disabled")
            document = self._attachment_loader.load(record)
            logger.info("Confluence attachment loaded source_id=%s", record.id)
            return document
        content_id = content_id_from_record(record)
        content = self._content.get_content(content_id)
        self._validate_content(content, content_id)

        if not self._should_process_content(content):
            raise DocumentProcessingError(
                f"Confluence content {content_id} does not match content filters"
            )
        include_comments = bool(
            self.config.include_comments and record.metadata.get("include_comments", True)
        )
        comments = self._content.fetch_comments(content_id) if include_comments else []
        attachments = []
        include_attachments = bool(
            self.config.include_attachments
            and record.metadata.get("include_attachments", True)
            and not record.metadata.get("defer_attachments", False)
        )
        if include_attachments:
            attachments = self._attachments.process(self._content.list_attachments(content_id))

        metadata = build_document_metadata(
            content,
            comments=comments,
            attachments=attachments,
            max_child_pages=self.config.max_child_pages,
        )
        body_html = body_html_from_content(content)
        source_url = display_url(
            self.base_url,
            self.config.deployment,
            metadata.space_key,
            metadata.content_id,
            metadata.title,
        )

        metadata_payload = metadata.to_dict()
        metadata_payload["relations"] = self._relations.merge(
            list(record.metadata.get("relations") or ()),
            html=body_html,
            current_space=metadata.space_key,
            source_version=str(metadata.version),
        )
        metadata_payload["attachment_names"] = list(record.metadata.get("attachment_names") or ())
        document = RawDocument(
            id=record.id,
            source=source_url,
            content=body_html,
            content_type="text/html",
            metadata=metadata_payload,
            raw=content,
        )
        logger.info(
            "Confluence content loaded content_id=%s comments=%d attachments=%d content_chars=%d",
            content_id,
            len(comments),
            len(attachments),
            len(body_html),
        )
        return document

    def describe(
        self,
        record: SourceRecord,
    ) -> ConnectorDocumentDescriptor:
        """Discover comment/attachment versions and structural relations."""

        descriptor = self._descriptors.describe(record)
        logger.info(
            "Confluence content described source_id=%s comments=%d attachments=%d "
            "relations=%d bound_records=%d",
            record.id,
            len(descriptor.admission.comments),
            len(descriptor.admission.attachments),
            len(descriptor.admission.relations),
            len(descriptor.bound_records),
        )
        return descriptor

    def load_by_ids(self, content_ids: list[str]) -> Iterator[RawDocument]:
        """Load content for callers that already have Confluence IDs."""
        for content_id in content_ids:
            yield self.load(self._record_for_id(content_id, ConnectorQuery()))

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
        record.metadata[DISCOVERY_DESCRIPTOR_KEY] = content
        return self._apply_query_policy(record, query)
