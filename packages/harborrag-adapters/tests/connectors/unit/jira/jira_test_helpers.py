"""Shared fake client, parser, and fixture builders for Jira connector tests."""

from __future__ import annotations

from typing import Any

from harborrag_adapters.connectors.jira import JiraProjectConfig
from harborrag_core.domain.parser import ParsedDocument

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

    def download_bytes(self, url: str, *, max_bytes: int | None = None) -> bytes | None:
        del max_bytes
        return self.downloads.get(url)


class FakeAttachmentParser:
    def parse(self, parse_input) -> ParsedDocument:
        return ParsedDocument(content=f"parsed:{parse_input.filename}", parser_name="fake")


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
            "customfield_10010": {"value": "Platform"},
            "customfield_10011": [
                {"value": "Docs"},
                {"value": "Search"},
            ],
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
        "names": {
            "customfield_10010": "Impact Area",
            "customfield_10011": "Teams",
        },
        "schema": {
            "customfield_10010": {
                "type": "option",
                "custom": "com.atlassian.jira.plugin.system.customfieldtypes:select",
            },
            "customfield_10011": {
                "type": "array",
                "custom": "com.atlassian.jira.plugin.system.customfieldtypes:multiselect",
            },
        },
    }
