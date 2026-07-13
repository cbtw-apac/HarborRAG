"""Shared fake client, parser, and fixture builders for Confluence connector tests."""

from __future__ import annotations

from typing import Any

from harborrag_adapters.connectors.confluence import ConfluenceSpaceConfig
from harborrag_core.domain.parser import ParsedDocument

CLOUD_BASE = "https://example.atlassian.net/wiki"
DC_BASE = "https://confluence.example.com"


class FakeConfluenceClient:
    def __init__(self) -> None:
        self.responses: dict[str, list[dict[str, Any]]] = {}
        self.downloads: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def add(self, endpoint: str, *responses: dict[str, Any]) -> None:
        self.responses.setdefault(endpoint, []).extend(responses)

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((endpoint, params))
        values = self.responses.get(endpoint)
        if not values:
            raise AssertionError(f"Unexpected Confluence endpoint: {endpoint}")
        return values.pop(0)

    def download_bytes(self, url: str) -> bytes | None:
        return self.downloads.get(url)


class FakeAttachmentParser:
    def parse(self, parse_input) -> ParsedDocument:
        return ParsedDocument(
            content=f"parsed:{parse_input.filename}",
            parser_name="fake",
        )


def cloud_config(**overrides: Any) -> ConfluenceSpaceConfig:
    values = {
        "space_key": "ENG",
        "base_url": CLOUD_BASE,
        "token": "token",
        "email": "me@example.com",
        "requests_per_minute": 6000,
        "page_size": 2,
    }
    values.update(overrides)
    return ConfluenceSpaceConfig(**values)


def dc_config(**overrides: Any) -> ConfluenceSpaceConfig:
    values = {
        "space_key": "ENG",
        "base_url": DC_BASE,
        "token": "pat",
        "requests_per_minute": 6000,
        "page_size": 2,
    }
    values.update(overrides)
    return ConfluenceSpaceConfig(**values)


def light_content(
    content_id: str,
    title: str,
    labels: list[str] | None = None,
    *,
    space_key: str = "ENG",
) -> dict:
    return {
        "id": content_id,
        "title": title,
        "type": "page",
        "space": {"key": space_key},
        "metadata": {"labels": {"results": [{"name": name} for name in labels or []]}},
        "version": {"when": "2024-05-24T20:57:56.130Z"},
    }


def full_content(content_id: str = "1") -> dict:
    return {
        "id": content_id,
        "title": "Page One",
        "type": "page",
        "space": {"key": "ENG"},
        "version": {
            "number": 3,
            "when": "2024-05-24T20:57:56.130Z",
            "by": {"displayName": "Bob"},
        },
        "history": {
            "createdBy": {"displayName": "Alice"},
            "createdDate": "2023-01-01T00:00:00.000Z",
        },
        "metadata": {"labels": {"results": [{"name": "runbook"}]}},
        "body": {"export_view": {"value": "<p>Hello <b>World</b></p>"}},
        "ancestors": [{"id": "0", "title": "Root", "type": "page"}],
        "children": {"page": {"results": [{"id": "9", "title": "Child"}]}},
    }
