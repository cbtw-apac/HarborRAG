"""JIRA issue discovery and raw-document loading orchestration."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from harborrag_adapters.connectors.attachments import AttachmentProcessor
from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.schemas import ConnectorCapabilities, ConnectorQuery
from harborrag_adapters.connectors.utils import enforce_collection_limit
from harborrag_adapters.parsers import HarborParser

from .client import JiraClient, _RequestsJiraClient
from .config import JiraProjectConfig
from .content import build_raw_content
from .issues import JiraIssueAPI
from .mappers import (
    build_document_metadata,
    build_source_record,
    issue_key_from_record,
    issue_url,
)
from .utils import build_jql

logger = logging.getLogger("harborrag.adapters.connectors.jira")
_TIME_FOR_JIRA_CONNECTOR_TESTS = time


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
    )

    def __init__(
        self,
        config: JiraProjectConfig,
        *,
        client: JiraClient | None = None,
        parser: HarborParser | None = None,
    ) -> None:
        """Initialize issue operations and shared attachment processing."""
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.client = client or _RequestsJiraClient(config)
        self._issues = JiraIssueAPI(self.client, config)
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
        """Search JIRA issues or materialize explicitly requested issue keys."""
        query = query or ConnectorQuery()
        issue_keys = self._issue_keys_from_query(query)
        if issue_keys:
            for issue_key in issue_keys[: query.limit]:
                yield self._record_for_key(issue_key, query)
            return

        yielded = 0
        for issue in self._issues.search(self._jql_from_query(query)):
            record = build_source_record(issue, base_url=self.base_url)
            record.metadata["include_attachments"] = query.include_attachments
            yield record
            yielded += 1
            if query.limit is not None and yielded >= query.limit:
                return

    def load(self, record: SourceRecord) -> RawDocument:
        """Load one JIRA issue as a text/markdown raw document."""
        issue_key = issue_key_from_record(record)
        issue = self._issues.get_issue(issue_key)
        self._validate_issue(issue, issue_key)

        comments = (
            self._issues.fetch_comments(issue_key)
            if self.config.include_comments
            else []
        )
        changelog = (
            self._issues.fetch_changelog(issue_key)
            if self.config.include_changelog
            else []
        )
        attachments = []
        include_attachments = bool(
            self.config.include_attachments
            and record.metadata.get("include_attachments", True)
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

        return RawDocument(
            id=record.id,
            source=issue_url(self.base_url, issue_key),
            content=content,
            content_type="text/markdown",
            metadata=metadata.to_dict(),
            raw=issue,
        )

    def load_by_keys(self, issue_keys: list[str]) -> Iterator[RawDocument]:
        """Load issues for callers that already have issue keys."""
        for issue_key in issue_keys:
            yield self.load(self._record_for_key(issue_key, ConnectorQuery()))

    def _jql_from_query(self, query: ConnectorQuery) -> str:
        """Translate shared connector filters into safe JIRA JQL."""
        filters = query.filters
        return build_jql(
            project_keys=self._list_filter(
                filters.get("project_keys") or filters.get("project_key") or query.path,
                default=self.config.project_keys,
            ),
            issue_types=self._list_filter(
                filters.get("issue_types") or filters.get("issue_type"),
                default=self.config.issue_types,
            ),
            statuses=self._list_filter(
                filters.get("statuses") or filters.get("status"),
                default=self.config.statuses,
            ),
            labels=self._list_filter(
                filters.get("labels") or filters.get("label"),
                default=self.config.labels,
            ),
            updated_after=query.updated_after,
            raw_jql=filters.get("jql") or query.pattern,
        )

    @staticmethod
    def _issue_keys_from_query(query: ConnectorQuery) -> list[str]:
        values = query.filters.get("issue_keys") or query.filters.get("keys")
        if values is None:
            return []
        if isinstance(values, str):
            return [values]
        return [str(value) for value in values]

    @staticmethod
    def _list_filter(value: Any, *, default: list[str]) -> list[str]:
        if value is None:
            return list(default)
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]

    def _record_for_key(self, issue_key: str, query: ConnectorQuery) -> SourceRecord:
        """Build a direct-load record when discovery is driven by issue keys."""
        project_key = issue_key.split("-", 1)[0]
        return SourceRecord(
            id=f"jira://{project_key}/{issue_key}",
            source_type="application/vnd.atlassian.jira.issue+json",
            locator=issue_key,
            metadata={
                "issue_key": issue_key,
                "project_key": project_key,
                "url": issue_url(self.base_url, issue_key),
                "include_attachments": query.include_attachments,
            },
        )

    @staticmethod
    def _validate_issue(issue: dict[str, Any], issue_key: str) -> None:
        """Fail fast when JIRA omits fields required by mappers."""
        missing = [
            name
            for name, value in (
                ("id", issue.get("id")),
                ("key", issue.get("key")),
                ("fields.summary", issue.get("fields", {}).get("summary")),
            )
            if not value
        ]
        if missing:
            raise DocumentProcessingError(
                f"JIRA issue {issue_key} missing required fields: "
                f"{', '.join(missing)}"
            )
