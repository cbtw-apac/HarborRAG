"""Jira query translation, source policy, and scope validation."""

from __future__ import annotations

from typing import Any

from harborrag_adapters.connectors.attachments import attachment_ids_from_filters
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.query_values import normalized_string_list
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord

from .config import JiraProjectConfig
from .mappers import issue_url
from .query import build_jql, validate_issue_key


def issue_keys_from_query(query: ConnectorQuery) -> list[str]:
    values = query.filters.get("issue_keys") or query.filters.get("keys")
    if values is None:
        return []
    if isinstance(values, str):
        return [validate_issue_key(values)]
    return [validate_issue_key(str(value)) for value in values]


class JiraDiscoveryPolicy:
    def __init__(self, config: JiraProjectConfig, *, base_url: str) -> None:
        self._config = config
        self._base_url = base_url

    def jql(self, query: ConnectorQuery) -> str:
        filters = query.filters
        return build_jql(
            project_keys=normalized_string_list(
                filters.get("project_keys") or filters.get("project_key") or query.path,
                default=self._config.project_keys,
            ),
            issue_types=normalized_string_list(
                filters.get("issue_types") or filters.get("issue_type"),
                default=self._config.issue_types,
            ),
            statuses=normalized_string_list(
                filters.get("statuses") or filters.get("status"),
                default=self._config.statuses,
            ),
            labels=normalized_string_list(
                filters.get("labels") or filters.get("label"),
                default=self._config.labels,
            ),
            updated_after=query.updated_after,
            text_search=query.pattern,
            raw_jql=filters.get("jql"),
        )

    def record_for_key(self, issue_key: str, query: ConnectorQuery) -> SourceRecord:
        issue_key = validate_issue_key(issue_key)
        project_key = issue_key.split("-", 1)[0]
        self._check_project_scope(project_key, issue_key)
        return self.apply_query(
            SourceRecord(
                id=f"jira://{project_key}/{issue_key}",
                source_type="application/vnd.atlassian.jira.issue+json",
                locator=issue_key,
                metadata={
                    "issue_key": issue_key,
                    "project_key": project_key,
                    "url": issue_url(self._base_url, issue_key),
                    "include_attachments": query.include_attachments,
                },
            ),
            query,
        )

    @staticmethod
    def apply_query(record: SourceRecord, query: ConnectorQuery) -> SourceRecord:
        record.metadata["include_attachments"] = query.include_attachments
        record.metadata["include_comments"] = bool(query.filters.get("include_comments", True))
        record.metadata["build_graph"] = bool(query.filters.get("build_graph", True))
        selected = attachment_ids_from_filters(query.filters)
        if selected:
            record.metadata["_selected_attachment_ids"] = selected
        return record

    def validate_issue(self, issue: dict[str, Any], issue_key: str) -> None:
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
        self._check_project_scope(fields.get("project", {}).get("key"), issue_key)

    def _check_project_scope(self, project_key: str | None, issue_key: str) -> None:
        if self._config.project_keys and project_key not in self._config.project_keys:
            raise DocumentProcessingError(
                f"JIRA issue {issue_key} belongs to project {project_key!r}, "
                f"outside configured projects {self._config.project_keys!r}"
            )
