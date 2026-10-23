"""Confluence connector for discovery, description and document loading.

Handles:
- CQL and explicit-ID discovery over Cloud cursors and Data Center offsets
- Admission descriptors through ``ConfluenceDescriptorBuilder``
- Page loading as HTML raw documents with comments and relations
- Attachment loading gated on the parent page's ACL, re-read before download
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from harborrag_adapters.connectors.acl import (
    SOURCE_ACL_METADATA_KEY,
    RestrictedContentGate,
    SourceAcl,
    acl_metadata,
)
from harborrag_adapters.connectors.atlassian.realm import atlassian_realm
from harborrag_adapters.connectors.attachments import (
    AttachmentDocumentLoader,
    AttachmentProcessor,
    AttachmentSourceGateway,
    AttachmentSourcePolicy,
    attachment_parent_locator,
    is_attachment_record,
)
from harborrag_adapters.connectors.base import BaseConnector, replay_discovery_page
from harborrag_adapters.connectors.descriptors import ConnectorDocumentDescriptor
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.policies.http import verify_credentials
from harborrag_adapters.connectors.rate_limiting import ConnectorRateLimiter
from harborrag_adapters.connectors.schemas import (
    ConnectorCapabilities,
    ConnectorPage,
    ConnectorQuery,
    ConnectorSkip,
)
from harborrag_adapters.parsers import HarborParserRegistry
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .access import ConfluenceAccessResolver
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
from .relations import ConfluenceSourceRelationResolver
from .title_resolution import PageTitleResolver

logger = logging.getLogger("harborrag.adapters.connectors.confluence")


class ConfluenceConnector(ConfluenceQueryPolicyMixin, BaseConnector):
    """Connector for Confluence Cloud and Data Center content.

    Discovery returns page and blogpost records; loading fetches the expanded body and optional
    comments and attachments as one document.
    """

    provider_name = "confluence"
    capabilities = ConnectorCapabilities(
        pagination=True,
        attachments=True,
        comments=True,
        labels=True,
        permissions=True,
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
        """Wire the content API, attachment handling and ACL collaborators.

        Args:
            config: Validated space configuration.
            client: HTTP client; a requests-based client is built when omitted.
            parser: Parser registry for attachments.
            rate_limiter: Limiter for the default client.
        """
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
        self._access = ConfluenceAccessResolver(
            self._content,
            realm=atlassian_realm(self.base_url),
            scope=f"confluence-space:{config.space_key}",
        )
        self._gate = RestrictedContentGate(config.restricted_content)
        self._descriptors = ConfluenceDescriptorBuilder(
            content=self._content,
            attachments=attachment_sources,
            config=config,
            access=self._access,
            gate=self._gate,
        )
        self._relations = ConfluenceSourceRelationResolver()
        self._titles = PageTitleResolver(self._content)

    def close(self) -> None:
        """Close the client when it exposes ``close``."""

        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def connect(self) -> None:
        """Verify Confluence credentials once before the first discovery request.

        Raises ``AuthenticationError`` for a 401 and ``AuthorizationError`` for a 403,
        with a uniform provider message.
        """
        verify_credentials(lambda: self.client.get_json("user/current"), provider="Confluence")

    @property
    def skipped(self) -> tuple[ConnectorSkip, ...]:
        """Return pages the latest discovery withheld because of source access."""

        return self._gate.skipped

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Search Confluence content or materialize explicitly requested IDs.

        Each call starts a new scan, so cached ancestor restrictions are dropped.

        Args:
            query: Filters and limit; ``None`` searches the configured space.

        Returns:
            Iterator of source records.
        """
        self._access.reset()
        return self._discover_records(query)

    def _discover_records(self, query: ConnectorQuery | None) -> Iterator[SourceRecord]:
        """Yield records for explicit IDs or a CQL search, honoring ``query.limit``."""
        self._ensure_connected()
        query = query or ConnectorQuery()
        self._gate.reset()
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
                for record in self._explicit_records(content_ids, query):
                    yielded += 1
                    yield record
                    if query.limit is not None and yielded >= query.limit:
                        return
                return

            cql = self._cql_from_query(query)
            for content in self._content.search(cql):
                content_id = str(content.get("id") or "<unknown>")
                self._validate_content(content, content_id)
                if not self._discoverable(content, content_id):
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
        """Return one discovery page using Cloud cursors or Data Center offsets.

        ``cursor=None`` starts a new scan and drops cached ancestor restrictions; later pages,
        including explicit-ID replays, reuse them.

        Args:
            query: Filters; ``None`` searches the configured space.
            cursor: Cursor from the previous page, or None to start.
            page_size: Number of records wanted.

        Returns:
            Page of records with the next cursor, or None on the last page.
        """

        self._ensure_connected()
        query = query or ConnectorQuery()
        self._gate.reset()
        if cursor is None:
            self._access.reset()
        if self._content_ids_from_query(query):
            page = replay_discovery_page(
                self._discover_records, query, cursor=cursor, page_size=page_size
            )
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
                if not self._discoverable(content, content_id):
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

    def _explicit_records(
        self, content_ids: list[str], query: ConnectorQuery
    ) -> Iterator[SourceRecord]:
        """Resolve requested IDs and their children to records, withholding restricted pages."""

        if query.limit is not None and query.limit <= 0:
            return
        admitted = 0
        for content_id in self._content.with_children(content_ids, query):
            record = self._record_for_id(content_id, query)
            # Same label, live-doc and restricted-content filters as search.
            # Skipping a page still walks its children, which are judged alone.
            if not self._discoverable(record.metadata[DISCOVERY_DESCRIPTOR_KEY], content_id):
                logger.debug("Confluence content filtered out content_id=%s", content_id)
                continue
            yield record
            admitted += 1
            # Stop before pulling the next ID: `with_children` fetches child pages
            # lazily.
            if query.limit is not None and admitted >= query.limit:
                return

    def _discoverable(self, content: dict[str, Any], content_id: str) -> bool:
        """Return whether content passes the label, live-doc and restricted-content checks."""

        return self._should_process_content(content) and self._gate.admit(
            self._access.page_acl(content), path=content_id
        )

    def _require_parent_access(self, record: SourceRecord) -> SourceAcl:
        """Re-read the parent page's ACL before an attachment download and return it.

        Ancestor restrictions come from the scan cache when present.
        """

        parent_id = attachment_parent_locator(record)
        acl = self._access.page_acl(self._content.get_content_descriptor(parent_id))
        self._gate.require(acl, path=parent_id)
        return acl

    def load(self, record: SourceRecord) -> RawDocument:
        """Load one Confluence page, or one attachment, as a raw document.

        Pages become HTML documents with comments, relations and attachments unless deferred.
        Attachments re-read their parent page's ACL first.

        Args:
            record: Page or attachment record.

        Returns:
            Raw document carrying the ACL metadata of the page.

        Raises:
            DocumentProcessingError: If attachment loading is disabled, or the page is
                malformed, outside the configured space or no longer matches the content
                filters.
            SourceRestrictedError: If restricted-content policy withholds the page.
        """

        if is_attachment_record(record):
            if (
                not self.config.include_attachments
                or record.metadata.get("include_attachments") is False
            ):
                raise DocumentProcessingError("Confluence attachment loading is disabled")
            parent_acl = self._require_parent_access(record)
            document = self._attachment_loader.load(record)
            # The parent's ACL read just now replaces whatever the record carried.
            document.metadata.pop(SOURCE_ACL_METADATA_KEY, None)
            document.metadata.update(acl_metadata(parent_acl))
            logger.info("Confluence attachment loaded source_id=%s", record.id)
            return document
        content_id = content_id_from_record(record)
        content = self._content.get_content(content_id)
        self._validate_content(content, content_id)

        if not self._should_process_content(content):
            raise DocumentProcessingError(
                f"Confluence content {content_id} does not match content filters"
            )
        acl = self._access.page_acl(content)
        self._gate.require(acl, path=content_id)
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
            self._content.complete_children(content),
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

        metadata_payload = metadata.to_dict()
        metadata_payload["relations"] = self._relations.merge(
            list(record.metadata.get("relations") or ()),
            html=body_html,
            current_space=metadata.space_key,
            source_version=str(metadata.version),
            resolve_title=self._titles.page_id_for_title,
        )
        metadata_payload["attachment_names"] = list(record.metadata.get("attachment_names") or ())
        metadata_payload.update(acl_metadata(acl))
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
        """Return the admission descriptor for a page (versions, relations, bound attachments)."""

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
        """Yield raw documents for Confluence IDs the caller already has."""
        for content_id in content_ids:
            yield self.load(self._record_for_id(content_id, ConnectorQuery()))
