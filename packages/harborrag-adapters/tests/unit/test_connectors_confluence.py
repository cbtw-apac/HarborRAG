from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from harborrag_adapters.connectors.confluence import (
    ConfluenceConnector,
    ConfluenceDeploymentType,
    ConfluenceSpaceConfig,
)
from harborrag_adapters.connectors.confluence.utils import build_cql
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.source import SourceRecord


pytestmark = [pytest.mark.unit, pytest.mark.graybox]


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
    def parse(self, input) -> ParsedDocument:
        return ParsedDocument(
            content=f"parsed:{input.filename}",
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


def light_content(content_id: str, title: str, labels: list[str] | None = None) -> dict:
    return {
        "id": content_id,
        "title": title,
        "type": "page",
        "space": {"key": "ENG"},
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


def test_config_auto_detects_cloud_and_datacenter():
    assert cloud_config().deployment_type == ConfluenceDeploymentType.CLOUD
    assert dc_config().deployment_type == ConfluenceDeploymentType.DATACENTER


def test_config_requires_cloud_email_and_rejects_bad_content_type():
    with pytest.raises(ValueError, match="email is required"):
        ConfluenceSpaceConfig(space_key="ENG", base_url=CLOUD_BASE, token="token")
    with pytest.raises(ValueError, match="content_types"):
        cloud_config(content_types=["page", "comment"])


def test_build_cql_supports_incremental_sync_and_rejects_unsafe_tokens():
    cql = build_cql(
        space_key="ENG",
        content_types=["page"],
        labels=["runbook"],
        updated_after=datetime(2024, 1, 2, 3, 4, tzinfo=UTC),
    )

    assert 'space = "ENG"' in cql
    assert 'type in ("page")' in cql
    assert 'label in ("runbook")' in cql
    assert 'lastmodified >= "2024/01/02 03:04"' in cql
    with pytest.raises(ValueError):
        build_cql(space_key='ENG" OR space = "OTHER')


def test_discover_paginates_and_filters_excluded_labels():
    client = FakeConfluenceClient()
    client.add(
        "content/search",
        {
            "results": [
                light_content("1", "Keep"),
                light_content("2", "Skip", labels=["archived"]),
            ],
            "_links": {"next": "/rest/api/content/search?cursor=abc"},
        },
        {"results": [light_content("3", "Keep Also")], "_links": {}},
    )
    connector = ConfluenceConnector(
        cloud_config(exclude_labels=["archived"]),
        client=client,
    )

    records = list(connector.discover())

    assert [record.metadata["content_id"] for record in records] == ["1", "3"]
    assert records[0].id == "confluence://ENG/1"
    assert client.calls[1][1]["cursor"] == "abc"


def test_discover_supports_direct_content_ids_without_search():
    connector = ConfluenceConnector(cloud_config(), client=FakeConfluenceClient())

    records = list(
        connector.discover(
            ConnectorQuery(filters={"content_ids": ["1", "2"]}, limit=1)
        )
    )

    assert [record.locator for record in records] == ["1"]


def test_discover_can_expand_child_pages_for_direct_ids():
    client = FakeConfluenceClient()
    client.add(
        "content/1/child/page",
        {"results": [light_content("2", "Child")]},
    )
    client.add("content/2/child/page", {"results": []})
    connector = ConfluenceConnector(cloud_config(), client=client)

    records = list(
        connector.discover(
            ConnectorQuery(filters={"content_ids": ["1"], "include_children": True})
        )
    )

    assert [record.locator for record in records] == ["1", "2"]


def test_load_builds_raw_document_metadata_comments_and_attachments():
    client = FakeConfluenceClient()
    client.add("content/1", full_content())
    client.add(
        "content/1/child/comment",
        {
            "results": [
                {
                    "id": "c1",
                    "body": {"storage": {"value": "<p>Nice</p>"}},
                    "history": {"createdBy": {"displayName": "Carol"}},
                }
            ]
        },
    )
    client.add(
        "content/1/child/attachment",
        {
            "results": [
                {
                    "id": "a1",
                    "title": "notes.md",
                    "metadata": {"mediaType": "text/markdown"},
                    "extensions": {"fileSize": 12},
                    "_links": {"download": "/download/a1"},
                }
            ]
        },
    )
    client.downloads[f"{CLOUD_BASE}/download/a1"] = b"# Notes"
    connector = ConfluenceConnector(
        cloud_config(include_comments=True, include_attachments=True),
        client=client,
        parser=FakeAttachmentParser(),
    )

    document = connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))

    assert document.id == "confluence://ENG/1"
    assert document.content_type == "text/html"
    assert document.content == "<p>Hello <b>World</b></p>"
    assert document.source == f"{CLOUD_BASE}/spaces/ENG/pages/1"
    assert document.metadata["author"] == "Alice"
    assert document.metadata["breadcrumb"] == ["Root"]
    assert document.metadata["children"] == [
        {"id": "9", "title": "Child", "type": "page"}
    ]
    assert document.metadata["comments"][0]["author"] == "Carol"
    processed_attachments = [
        attachment
        for attachment in document.metadata["attachments"]
        if attachment["status"] == "processed"
    ]
    assert len(processed_attachments) == 1
    assert document.metadata["attachments"][0]["text"] == "parsed:notes.md"
    assert "attachments_summary" not in document.metadata
    assert "breadcrumb_text" not in document.metadata
    assert "canonical_url" not in document.metadata
    assert "display_url" not in document.metadata


def test_load_skips_cross_origin_attachment_download_urls():
    client = FakeConfluenceClient()
    client.add("content/1", full_content())
    client.add(
        "content/1/child/attachment",
        {
            "results": [
                {
                    "id": "a1",
                    "title": "notes.md",
                    "metadata": {"mediaType": "text/markdown"},
                    "extensions": {"fileSize": 12},
                    "_links": {"download": "https://evil.example/notes.md"},
                }
            ]
        },
    )
    connector = ConfluenceConnector(
        cloud_config(include_attachments=True),
        client=client,
        parser=FakeAttachmentParser(),
    )

    document = connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))

    skipped_attachments = [
        attachment
        for attachment in document.metadata["attachments"]
        if attachment["status"] == "skipped"
    ]
    assert len(skipped_attachments) == 1
    assert "outside trusted origin" in document.metadata["attachments"][0]["reason"]


def test_load_rejects_comment_pages_over_configured_limit():
    client = FakeConfluenceClient()
    client.add("content/1", full_content())
    client.add(
        "content/1/child/comment",
        {
            "results": [
                {"id": "c1", "body": {"storage": {"value": "one"}}},
                {"id": "c2", "body": {"storage": {"value": "two"}}},
            ]
        },
    )
    connector = ConfluenceConnector(
        cloud_config(include_comments=True, max_comments=1),
        client=client,
    )

    with pytest.raises(DocumentProcessingError, match="max_comments"):
        connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))


def test_load_rejects_attachment_pages_over_configured_limit():
    client = FakeConfluenceClient()
    client.add("content/1", full_content())
    client.add(
        "content/1/child/attachment",
        {
            "results": [
                {"id": "a1", "title": "one.md"},
                {"id": "a2", "title": "two.md"},
            ]
        },
    )
    connector = ConfluenceConnector(
        cloud_config(include_attachments=True, max_attachments=1),
        client=client,
    )

    with pytest.raises(DocumentProcessingError, match="max_attachments"):
        connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))


def test_load_raises_on_missing_required_fields():
    client = FakeConfluenceClient()
    content = full_content()
    del content["title"]
    client.add("content/1", content)
    connector = ConfluenceConnector(cloud_config(), client=client)

    with pytest.raises(DocumentProcessingError, match="title"):
        connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))
