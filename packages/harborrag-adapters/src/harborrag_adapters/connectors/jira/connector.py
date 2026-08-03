"""JIRA issue discovery and raw-document loading orchestration."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from harborrag_adapters.connectors.attachments import (
    AttachmentDocumentLoader,
    AttachmentProcessor,
    AttachmentSourceGateway,
    AttachmentSourcePolicy,
    is_attachment_record,
)
from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.descriptors import (
    ConnectorDocumentDescriptor,
)
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.policies.validation import enforce_collection_limit
from harborrag_adapters.connectors.rate_limiting import ConnectorRateLimiter
from harborrag_adapters.connectors.schemas import (
    ConnectorCapabilities,
    ConnectorPage,
    ConnectorQuery,
)
from harborrag_adapters.parsers import HarborParserRegistry
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .client import JiraClient, _RequestsJiraClient
from .config import JiraProjectConfig
from .content import build_raw_content
from .discovery import JiraDescriptorBuilder
from .discovery_policy import JiraDiscoveryPolicy, issue_keys_from_query
from .issues import DISCOVERY_DESCRIPTOR_KEY, JiraIssueAPI
from .mappers import (
    build_document_metadata,
    build_source_record,
    issue_key_from_record,
    issue_url,
)

logger = logging.getLogger("harborrag.adapters.connectors.jira")


class JiraConnector(BaseConnector):
    """Connector for JIRA Cloud and Data Center REST APIs.

    Discovery searches issues by JQL or direct issue keys. Loading a record
    fetches full issue fields plus optional comments, changelog, and attachment
    text into one Markdown-ish raw document.
    """

    provider_name = "jira"
    capabilities = ConnectorCapabilities(
        pagination=True,
        attachments=True,
        comments=True,
        incremental_sync=True,
        full_sync=True,
        relationships=True,
        labels=True,
        changelog=True,
        concurrent_describe=True,
    )

    def __init__(
        self,
        config: JiraProjectConfig,
        *,
        client: JiraClient | None = None,
        parser: HarborParserRegistry | None = None,
        rate_limiter: ConnectorRateLimiter | None = None,
    ) -> None:
        """Initialize issue operations and shared attachment processing."""
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.client = client or _RequestsJiraClient(
            config,
            rate_limiter=rate_limiter,
        )
        self._issues = JiraIssueAPI(self.client, config)
        self._policy = JiraDiscoveryPolicy(config, base_url=self.base_url)
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
        self._descriptors = JiraDescriptorBuilder(
            issues=self._issues,
            attachments=attachment_sources,
            config=config,
            base_url=self.base_url,
        )

    def close(self) -> None:
        """Release the client session when the connector owns one."""

        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Search JIRA issues or materialize explicitly requested issue keys."""
        query = query or ConnectorQuery()
        issue_keys = issue_keys_from_query(query)
        yielded = 0
        mode = "explicit" if issue_keys else "search"
        logger.info(
            "JIRA discovery started mode=%s limit=%s",
            mode,
            query.limit,
        )
        try:
            if issue_keys:
                for issue_key in issue_keys[: query.limit]:
                    record = self._policy.record_for_key(issue_key, query)
                    yielded += 1
                    yield record
                return

            for issue in self._issues.search(self._policy.jql(query)):
                issue_key = str(issue.get("key") or "")
                self._policy.validate_issue(issue, issue_key)
                record = build_source_record(issue, base_url=self.base_url)
                record.metadata[DISCOVERY_DESCRIPTOR_KEY] = issue
                record = self._policy.apply_query(record, query)
                yielded += 1
                yield record
                if query.limit is not None and yielded >= query.limit:
                    return
        finally:
            logger.info(
                "JIRA discovery iterator closed mode=%s yielded=%d",
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
        """Use Jira's native page token or offset without replaying prior pages."""

        query = query or ConnectorQuery()
        if issue_keys_from_query(query):
            page = super().discover_page(query, cursor=cursor, page_size=page_size)
            logger.debug(
                "JIRA discovery page mode=explicit records=%d has_next=%s",
                len(page.records),
                page.next_cursor is not None,
            )
            return page
        output: list[SourceRecord] = []
        next_cursor = cursor
        jql = self._policy.jql(query)
        while len(output) < page_size:
            issues, next_cursor = self._issues.search_page(
                jql,
                cursor=next_cursor,
                limit=page_size - len(output),
            )
            for issue in issues:
                issue_key = str(issue.get("key") or "")
                self._policy.validate_issue(issue, issue_key)
                record = build_source_record(issue, base_url=self.base_url)
                record.metadata[DISCOVERY_DESCRIPTOR_KEY] = issue
                output.append(self._policy.apply_query(record, query))
            if next_cursor is None:
                break
        page = ConnectorPage(tuple(output), next_cursor)
        logger.debug(
            "JIRA discovery page mode=search records=%d has_next=%s",
            len(page.records),
            page.next_cursor is not None,
        )
        return page

    def load(self, record: SourceRecord) -> RawDocument:
        """Load one JIRA issue as a text/markdown raw document."""

        if is_attachment_record(record):
            if (
                not self.config.include_attachments
                or record.metadata.get("include_attachments") is False
            ):
                raise DocumentProcessingError("JIRA attachment loading is disabled")
            document = self._attachment_loader.load(record)
            logger.info("JIRA attachment loaded source_id=%s", record.id)
            return document
        issue_key = issue_key_from_record(record)
        issue = self._issues.get_issue(issue_key)
        self._policy.validate_issue(issue, issue_key)

        include_comments = bool(
            self.config.include_comments and record.metadata.get("include_comments", True)
        )
        comments = self._issues.fetch_comments(issue_key) if include_comments else []
        changelog = self._issues.fetch_changelog(issue_key) if self.config.include_changelog else []
        attachments = []
        include_attachments = bool(
            self.config.include_attachments
            and record.metadata.get("include_attachments", True)
            and not record.metadata.get("defer_attachments", False)
        )
        if include_attachments:
            raw_attachments = issue.get("fields", {}).get("attachment") or []
            enforce_collection_limit(
                count=len(raw_attachments),
                limit=self.config.max_attachments,
                label=f"JIRA attachments for {issue_key}",
                setting_name="max_attachments",
            )
            attachments = self._attachments.process(raw_attachments)

        content = build_raw_content(
            issue,
            comments=comments,
            attachments=attachments,
            include_attachment_text=self.config.include_attachment_text_in_content,
        )
        metadata = build_document_metadata(
            issue,
            content=content,
            comments=comments,
            attachments=attachments,
            changelog=changelog,
        )

        metadata_payload = metadata.to_dict()
        metadata_payload["relations"] = list(record.metadata.get("relations") or ())
        metadata_payload["attachment_names"] = list(record.metadata.get("attachment_names") or ())
        document = RawDocument(
            id=record.id,
            source=issue_url(self.base_url, issue_key),
            content=content,
            content_type="text/markdown",
            metadata=metadata_payload,
            raw=issue,
        )
        logger.info(
            "JIRA issue loaded issue_key=%s comments=%d attachments=%d "
            "changelog_items=%d content_chars=%d",
            issue_key,
            len(comments),
            len(attachments),
            len(changelog),
            len(content),
        )
        return document

    def describe(
        self,
        record: SourceRecord,
    ) -> ConnectorDocumentDescriptor:
        """Discover comment/attachment versions and explicit issue relations."""

        descriptor = self._descriptors.describe(record)
        logger.info(
            "JIRA issue described source_id=%s comments=%d attachments=%d "
            "relations=%d bound_records=%d",
            record.id,
            len(descriptor.admission.comments),
            len(descriptor.admission.attachments),
            len(descriptor.admission.relations),
            len(descriptor.bound_records),
        )
        return descriptor

    def load_by_keys(self, issue_keys: list[str]) -> Iterator[RawDocument]:
        """Load issues for callers that already have issue keys."""
        for issue_key in issue_keys:
            yield self.load(self._policy.record_for_key(issue_key, ConnectorQuery()))

    def _jql_from_query(self, query: ConnectorQuery) -> str:
        """Translate shared connector filters into safe JIRA JQL."""
        return self._policy.jql(query)

    @staticmethod
    def _issue_keys_from_query(query: ConnectorQuery) -> list[str]:
        return issue_keys_from_query(query)
