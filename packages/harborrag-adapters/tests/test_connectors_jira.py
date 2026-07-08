from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from harborrag_adapters.connectors.jira import (
    JiraConnector,
    JiraDeploymentType,
    JiraProjectConfig,
)
from harborrag_adapters.connectors.jira.utils import build_jql
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.source import SourceRecord


CLOUD_BASE = "https://example.atlassian.net"
DC_BASE = "https://jira.example.com"


class FakeJiraClient:
    def __init__(self) -> None:
        self.get_responses: dict[str, list[dict[str, Any]]] = {}
        self.post_responses: dict[str, list[dict[str, Any]]] = {}
        self.downloads: dict[str, bytes] = {}
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    def add_get(self, endpoint: str, *responses: dict[str, Any]) -> None:
        self.get_responses.setdefault(endpoint, []).extend(responses)

    def add_post(self, endpoint: str, *responses: dict[str, Any]) -> None:
        self.post_responses.setdefault(endpoint, []).extend(responses)

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.get_calls.append((endpoint, params))
        values = self.get_responses.get(endpoint)
        if not values:
            raise AssertionError(f"Unexpected JIRA GET endpoint: {endpoint}")
        return values.pop(0)

    def post_json(self, endpoint: str, *, json: dict[str, Any]) -> dict[str, Any]:
        self.post_calls.append((endpoint, json))
        values = self.post_responses.get(endpoint)
        if not values:
            raise AssertionError(f"Unexpected JIRA POST endpoint: {endpoint}")
        return values.pop(0)

    def download_bytes(self, url: str) -> bytes | None:
        return self.downloads.get(url)


class FakeAttachmentParser:
    def parse(self, input) -> ParsedDocument:
        return ParsedDocument(content=f"parsed:{input.filename}", parser_name="fake")


def cloud_config(**overrides: Any) -> JiraProjectConfig:
    values = {
        "base_url": CLOUD_BASE,
        "token": "token",
        "email": "me@example.com",
        "project_keys": ["ENG"],
        "requests_per_minute": 6000,
        "page_size": 2,
    }
    values.update(overrides)
    return JiraProjectConfig(**values)


def dc_config(**overrides: Any) -> JiraProjectConfig:
    values = {
        "base_url": DC_BASE,
        "token": "pat",
        "project_keys": ["ENG"],
        "requests_per_minute": 6000,
        "page_size": 2,
    }
    values.update(overrides)
    return JiraProjectConfig(**values)


def issue(key: str = "ENG-1") -> dict[str, Any]:
    return {
        "id": "10001",
        "key": key,
        "fields": {
            "summary": "Build parser",
            "description": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "ADF body"}],
                    }
                ],
            },
            "issuetype": {"name": "Task"},
            "status": {"name": "In Progress", "statusCategory": {"name": "Doing"}},
            "priority": {"name": "High"},
            "assignee": {"displayName": "Ada"},
            "reporter": {"displayName": "Grace"},
            "creator": {"displayName": "Linus"},
            "labels": ["rag"],
            "project": {"key": "ENG", "name": "Engineering"},
            "components": [{"name": "Adapters"}],
            "fixVersions": [{"name": "1.0"}],
            "versions": [{"name": "0.9"}],
            "created": "2024-01-01T00:00:00.000+0000",
            "updated": "2024-05-24T20:57:56.130+0000",
            "resolutiondate": None,
            "duedate": "2024-06-01",
            "parent": {"id": "10000", "key": "ENG-0", "fields": {"summary": "Epic"}},
            "subtasks": [],
            "issuelinks": [],
            "attachment": [
                {
                    "id": "a1",
                    "filename": "notes.md",
                    "mimeType": "text/markdown",
                    "size": 12,
                    "content": f"{CLOUD_BASE}/secure/attachment/a1/notes.md",
                }
            ],
        },
    }


def test_config_auto_detects_cloud_and_datacenter():
    assert cloud_config().deployment_type == JiraDeploymentType.CLOUD
    assert dc_config().deployment_type == JiraDeploymentType.DATACENTER


def test_config_requires_cloud_email():
    with pytest.raises(ValueError, match="email is required"):
        JiraProjectConfig(base_url=CLOUD_BASE, token="token")


def test_build_jql_supports_incremental_sync_and_rejects_bad_project_key():
    jql = build_jql(
        project_keys=["ENG"],
        issue_types=["Task"],
        statuses=["In Progress"],
        labels=["rag"],
        updated_after=datetime(2024, 1, 2, 3, 4, tzinfo=UTC),
    )

    assert 'project in ("ENG")' in jql
    assert 'issuetype in ("Task")' in jql
    assert 'status in ("In Progress")' in jql
    assert 'labels in ("rag")' in jql
    assert 'updated >= "2024/01/02 03:04"' in jql
    assert "order by updated ASC, key ASC" in jql
    with pytest.raises(ValueError):
        build_jql(project_keys=['ENG" OR project = "OPS'])


def test_discover_searches_jql_with_start_at_pagination():
    client = FakeJiraClient()
    client.add_post(
        "search",
        {"startAt": 0, "total": 3, "issues": [issue("ENG-1"), issue("ENG-2")]},
        {"startAt": 2, "total": 3, "issues": [issue("ENG-3")]},
    )
    connector = JiraConnector(cloud_config(), client=client)

    records = list(connector.discover())

    assert [record.metadata["issue_key"] for record in records] == [
        "ENG-1",
        "ENG-2",
        "ENG-3",
    ]
    assert client.post_calls[1][1]["startAt"] == 2
    assert records[0].id == "jira://ENG/ENG-1"


def test_discover_supports_direct_issue_keys_without_search():
    connector = JiraConnector(cloud_config(), client=FakeJiraClient())

    records = list(
        connector.discover(ConnectorQuery(filters={"issue_keys": ["ENG-1"]}))
    )

    assert records[0].locator == "ENG-1"
    assert records[0].metadata["url"] == f"{CLOUD_BASE}/browse/ENG-1"


def test_load_builds_raw_document_comments_attachments_and_changelog():
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", issue())
    client.add_get(
        "issue/ENG-1/comment",
        {
            "startAt": 0,
            "total": 1,
            "comments": [
                {
                    "id": "c1",
                    "author": {"displayName": "Bob"},
                    "body": "Looks good",
                    "created": "2024-05-01T00:00:00.000+0000",
                }
            ],
        },
    )
    client.add_get(
        "issue/ENG-1/changelog",
        {
            "startAt": 0,
            "total": 1,
            "values": [
                {
                    "id": "h1",
                    "author": {"displayName": "Ada"},
                    "created": "2024-05-02T00:00:00.000+0000",
                    "items": [{"field": "status", "fromString": "Open", "toString": "Done"}],
                }
            ],
        },
    )
    client.downloads[f"{CLOUD_BASE}/secure/attachment/a1/notes.md"] = b"# Notes"
    connector = JiraConnector(
        cloud_config(include_attachments=True, include_changelog=True),
        client=client,
        parser=FakeAttachmentParser(),
    )

    document = connector.load(SourceRecord("jira://ENG/ENG-1", "jira", "ENG-1"))

    assert document.id == "jira://ENG/ENG-1"
    assert document.content_type == "text/markdown"
    assert "# ENG-1 Build parser" in document.content
    assert "ADF body" in document.content
    assert "Bob: Looks good" in document.content
    assert "parsed:notes.md" in document.content
    assert document.source == f"{CLOUD_BASE}/browse/ENG-1"
    assert document.metadata["assignee"] == "Ada"
    assert document.metadata["reporter"] == "Grace"
    assert document.metadata["attachments_summary"]["processed"] == 1
    assert document.metadata["changelog"][0]["items"][0]["field"] == "status"


def test_load_skips_cross_origin_attachment_download_urls():
    bad_issue = issue()
    bad_issue["fields"]["attachment"][0]["content"] = "https://evil.example/notes.md"
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", bad_issue)
    client.add_get("issue/ENG-1/comment", {"startAt": 0, "total": 0, "comments": []})
    connector = JiraConnector(
        cloud_config(include_attachments=True),
        client=client,
        parser=FakeAttachmentParser(),
    )

    document = connector.load(SourceRecord("jira://ENG/ENG-1", "jira", "ENG-1"))

    assert document.metadata["attachments_summary"]["skipped"] == 1
    assert "outside trusted origin" in document.metadata["attachments"][0]["reason"]


def test_load_rejects_comments_over_configured_limit():
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", issue())
    client.add_get(
        "issue/ENG-1/comment",
        {
            "startAt": 0,
            "total": 2,
            "comments": [
                {"id": "c1", "body": "one"},
                {"id": "c2", "body": "two"},
            ],
        },
    )
    connector = JiraConnector(
        cloud_config(max_comments=1),
        client=client,
    )

    with pytest.raises(DocumentProcessingError, match="max_comments"):
        connector.load(SourceRecord("jira://ENG/ENG-1", "jira", "ENG-1"))


def test_load_rejects_attachments_over_configured_limit():
    loaded_issue = issue()
    loaded_issue["fields"]["attachment"].append(
        {
            "id": "a2",
            "filename": "two.md",
            "mimeType": "text/markdown",
            "size": 12,
            "content": f"{CLOUD_BASE}/secure/attachment/a2/two.md",
        }
    )
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", loaded_issue)
    connector = JiraConnector(
        cloud_config(
            include_comments=False,
            include_attachments=True,
            max_attachments=1,
        ),
        client=client,
    )

    with pytest.raises(DocumentProcessingError, match="max_attachments"):
        connector.load(SourceRecord("jira://ENG/ENG-1", "jira", "ENG-1"))


def test_load_raises_on_missing_required_fields():
    bad = issue()
    bad["fields"].pop("summary")
    client = FakeJiraClient()
    client.add_get("issue/ENG-1", bad)
    connector = JiraConnector(cloud_config(), client=client)

    with pytest.raises(DocumentProcessingError, match="summary"):
        connector.load(SourceRecord("jira://ENG/ENG-1", "jira", "ENG-1"))
