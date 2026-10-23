"""Jira connector: issue discovery, description and loading.

Handles:
- Issue search by JQL or explicit keys, with native paging
- Source-access gating of issues and attachments
- Issue and attachment loading into raw documents

Does NOT build descriptors or run searches itself; ``JiraDescriptorBuilder`` and
``JiraIssueAPI`` do.
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
from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.descriptors import (
    ConnectorDocumentDescriptor,
)
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.policies.http import verify_credentials
from harborrag_adapters.connectors.policies.validation import truncate_with_limit
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

from .access import issue_acl
from .client import JiraClient, _RequestsJiraClient
from .comment_visibility import keep_verified_comments
from .config import JiraProjectConfig
from .content import build_raw_content
from .discovery import JiraDescriptorBuilder
from .discovery_policy import (
    JiraDiscoveryPolicy,
    issue_keys_from_query,
    verify_empty_search_result,
)
from .issues import DISCOVERY_DESCRIPTOR_KEY, JiraIssueAPI
from .mappers import (
    build_document_metadata,
    build_source_record,
    issue_key_from_record,
    issue_url,
)

logger = logging.getLogger("harborrag.adapters.connectors.jira")


class JiraConnector(BaseConnector):
    """Connector for Jira Cloud and Data Center issues.

    Closes its client, injected or not, in ``close``.
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
        permissions=True,
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
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self._realm = atlassian_realm(self.base_url)
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
        self._gate = RestrictedContentGate(config.restricted_content)
        self._descriptors = JiraDescriptorBuilder(
            issues=self._issues,
            attachments=attachment_sources,
            config=config,
            base_url=self.base_url,
            gate=self._gate,
            realm=self._realm,
        )

    def close(self) -> None:
        """Close the client session if the client has one."""
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def connect(self) -> None:
        """Verify the credentials with a ``myself`` request.

        Issue search answers 200 with no issues for a credential that lacks
        permission, so a bad token would otherwise look like an empty result.
        """
        verify_credentials(lambda: self.client.get_json("myself"), provider="JIRA")

    @property
    def skipped(self) -> tuple[ConnectorSkip, ...]:
        """Issues the latest discovery call withheld because of source access."""
        return self._gate.skipped

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Yield issue records from a JQL search or from explicit issue keys.

        In search mode, issues the source ACL withholds are skipped and listed in
        ``skipped``; explicit keys are checked when described or loaded. An empty
        search result is checked for a missing browse permission.

        Args:
            query: Filters and limit; ``issue_keys`` or ``keys`` selects explicit mode.

        Yields:
            One source record per issue; in search mode, only issues the ACL admits.

        Raises:
            AuthorizationError: If an empty search result is due to missing browse
                permission.
            DocumentProcessingError: If an issue is malformed or outside the
                configured projects.
        """
        self._ensure_connected()
        query = query or ConnectorQuery()
        self._gate.reset()
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
                if not self._gate.admit(issue_acl(issue, realm=self._realm), path=issue_key):
                    continue
                record = build_source_record(issue, base_url=self.base_url)
                record.metadata[DISCOVERY_DESCRIPTOR_KEY] = issue
                record = self._policy.apply_query(record, query)
                yielded += 1
                yield record
                if query.limit is not None and yielded >= query.limit:
                    return
            if yielded == 0:
                verify_empty_search_result(
                    query, policy=self._policy, issues=self._issues, logger_=logger
                )
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
        """Return one discovery page using Jira's native page token or offset.

        Args:
            query: Filters; explicit issue keys fall back to the base class paging.
            cursor: Cursor from the previous page, or ``None`` for the first.
            page_size: Maximum records on the page.

        Returns:
            Page of records and the next cursor, ``None`` on the last page.

        Raises:
            AuthorizationError: If an empty result is due to missing browse permission.
            DocumentProcessingError: If an issue is malformed or outside the
                configured projects.
        """

        self._ensure_connected()
        query = query or ConnectorQuery()
        self._gate.reset()
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
                if not self._gate.admit(issue_acl(issue, realm=self._realm), path=issue_key):
                    continue
                record = build_source_record(issue, base_url=self.base_url)
                record.metadata[DISCOVERY_DESCRIPTOR_KEY] = issue
                output.append(self._policy.apply_query(record, query))
            if next_cursor is None:
                break
        if not output:
            verify_empty_search_result(
                query, policy=self._policy, issues=self._issues, logger_=logger
            )
        page = ConnectorPage(tuple(output), next_cursor)
        logger.debug(
            "JIRA discovery page mode=search records=%d has_next=%s",
            len(page.records),
            page.next_cursor is not None,
        )
        return page

    def _require_parent_access(self, record: SourceRecord) -> SourceAcl:
        """Re-read the parent issue's ACL and refuse the attachment if it is withheld."""

        parent_key = attachment_parent_locator(record)
        acl = issue_acl(self._issues.get_issue_descriptor(parent_key), realm=self._realm)
        self._gate.require(acl, path=parent_key)
        return acl

    def load(self, record: SourceRecord) -> RawDocument:
        """Load an issue, or an attachment record, as a raw document.

        An issue renders as Markdown with comments and attachment text per configuration;
        the changelog goes into metadata only. An attachment record is refused when
        attachments are disabled and takes the parent's freshly read ACL.

        Args:
            record: Issue or attachment source record.

        Returns:
            Raw document with source ACL metadata.

        Raises:
            SourceRestrictedError: If restricted-content policy withholds the issue
                or the attachment's parent.
            DocumentProcessingError: If attachment loading is disabled or the issue
                is malformed.
        """

        if is_attachment_record(record):
            if (
                not self.config.include_attachments
                or record.metadata.get("include_attachments") is False
            ):
                raise DocumentProcessingError("JIRA attachment loading is disabled")
            parent_acl = self._require_parent_access(record)
            document = self._attachment_loader.load(record)
            # The parent's ACL read just now replaces whatever the record carried.
            document.metadata.pop(SOURCE_ACL_METADATA_KEY, None)
            document.metadata.update(acl_metadata(parent_acl))
            logger.info("JIRA attachment loaded source_id=%s", record.id)
            return document
        issue_key = issue_key_from_record(record)
        issue = self._issues.get_issue(issue_key)
        self._policy.validate_issue(issue, issue_key)
        acl = issue_acl(issue, realm=self._realm)
        self._gate.require(acl, path=issue_key)

        include_comments = bool(
            self.config.include_comments and record.metadata.get("include_comments", True)
        )
        comments: list[dict[str, Any]] = []
        if include_comments:
            comments = self._issues.fetch_comments(issue_key)
            keep_verified_comments(issue, comments)
        changelog = self._issues.fetch_changelog(issue_key) if self.config.include_changelog else []
        attachments = []
        include_attachments = bool(
            self.config.include_attachments
            and record.metadata.get("include_attachments", True)
            and not record.metadata.get("defer_attachments", False)
        )
        if include_attachments:
            raw_attachments: list[dict[str, Any]] = []
            truncate_with_limit(
                raw_attachments,
                issue.get("fields", {}).get("attachment") or [],
                limit=self.config.max_attachments,
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
        metadata_payload.update(acl_metadata(acl))
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
        """Build the admission descriptor for one issue record."""

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
        """Yield the loaded raw document for each issue key."""
        for issue_key in issue_keys:
            yield self.load(self._policy.record_for_key(issue_key, ConnectorQuery()))

    def _jql_from_query(self, query: ConnectorQuery) -> str:
        return self._policy.jql(query)

    @staticmethod
    def _issue_keys_from_query(query: ConnectorQuery) -> list[str]:
        return issue_keys_from_query(query)
