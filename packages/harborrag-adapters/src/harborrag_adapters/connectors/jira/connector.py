"""JIRA issue discovery and raw-document loading orchestration."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from harborrag_adapters.connectors.attachments.processing import AttachmentProcessor
from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.policies.validation import truncate_with_limit
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
from .issues import JiraIssueAPI
from .mappers import (
    build_document_metadata,
    build_source_record,
    issue_key_from_record,
    issue_url,
)
from .query import build_jql, validate_issue_key

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
    )

    def __init__(
        self,
        config: JiraProjectConfig,
        *,
        client: JiraClient | None = None,
        parser: HarborParserRegistry | None = None,
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

    def close(self) -> None:
        """Release the client session when the connector owns one."""

        close = getattr(self.client, "close", None)
        if callable(close):
            close()

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
            issue_key = str(issue.get("key") or "")
            self._validate_issue(issue, issue_key)
            record = build_source_record(issue, base_url=self.base_url)
            record.metadata["include_attachments"] = query.include_attachments
            yield record
            yielded += 1
            if query.limit is not None and yielded >= query.limit:
                return

    def discover_page(
        self,
        query: ConnectorQuery | None,
        *,
        cursor: str | None,
        page_size: int,
    ) -> ConnectorPage:
        """Use Jira's native page token or offset without replaying prior pages."""

        query = query or ConnectorQuery()
        if self._issue_keys_from_query(query):
            return super().discover_page(query, cursor=cursor, page_size=page_size)
        output: list[SourceRecord] = []
        next_cursor = cursor
        jql = self._jql_from_query(query)
        while len(output) < page_size:
            issues, next_cursor = self._issues.search_page(
                jql,
                cursor=next_cursor,
                limit=page_size - len(output),
            )
            for issue in issues:
                issue_key = str(issue.get("key") or "")
                self._validate_issue(issue, issue_key)
                record = build_source_record(issue, base_url=self.base_url)
                record.metadata["include_attachments"] = query.include_attachments
                output.append(record)
            if next_cursor is None:
                break
        return ConnectorPage(tuple(output), next_cursor)

    def load(self, record: SourceRecord) -> RawDocument:
        """Load one JIRA issue as a text/markdown raw document."""
        issue_key = issue_key_from_record(record)
        issue = self._issues.get_issue(issue_key)
        self._validate_issue(issue, issue_key)

        comments = self._issues.fetch_comments(issue_key) if self.config.include_comments else []
        changelog = self._issues.fetch_changelog(issue_key) if self.config.include_changelog else []
        attachments = []
        include_attachments = bool(
            self.config.include_attachments and record.metadata.get("include_attachments", True)
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
            text_search=query.pattern,
            raw_jql=filters.get("jql"),
        )

    @staticmethod
    def _issue_keys_from_query(query: ConnectorQuery) -> list[str]:
        values = query.filters.get("issue_keys") or query.filters.get("keys")
        if values is None:
            return []
        if isinstance(values, str):
            return [validate_issue_key(values)]
        return [validate_issue_key(str(value)) for value in values]

    @staticmethod
    def _list_filter(value: Any, *, default: list[str]) -> list[str]:
        if value is None:
            return list(default)
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]

    def _record_for_key(self, issue_key: str, query: ConnectorQuery) -> SourceRecord:
        """Build a direct-load record when discovery is driven by issue keys."""
        issue_key = validate_issue_key(issue_key)
        project_key = issue_key.split("-", 1)[0]
        self._check_project_scope(project_key, issue_key)
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

    def _check_project_scope(self, project_key: str | None, issue_key: str) -> None:
        """Reject an issue whose project falls outside configured project_keys.

        Shared by both discovery paths: JQL search results (validated via
        ``_validate_issue``) and explicitly requested issue keys (validated
        here in ``_record_for_key``), since an explicit out-of-scope key is
        just as much an escape hatch around project scoping as a raw JQL
        override that omits a project filter -- mirrors
        ConfluenceConnector._validate_content's space check.
        """
        if self.config.project_keys and project_key not in self.config.project_keys:
            raise DocumentProcessingError(
                f"JIRA issue {issue_key} belongs to project {project_key!r}, "
                f"outside configured projects {self.config.project_keys!r}"
            )

    def _validate_issue(self, issue: dict[str, Any], issue_key: str) -> None:
        """Fail fast when JIRA omits required fields or is out of project scope."""
        fields = issue.get("fields", {})
        missing = [
            name
            for name, value in (
                ("id", issue.get("id")),
                ("key", issue.get("key")),
                ("fields.summary", fields.get("summary")),
            )
            if not value
        ]
        if missing:
            raise DocumentProcessingError(
                f"JIRA issue {issue_key} missing required fields: {', '.join(missing)}"
            )
        project_key = fields.get("project", {}).get("key")
        self._check_project_scope(project_key, issue_key)
