"""Unit tests for Confluence connector document loading."""

from __future__ import annotations

import pytest
from confluence_test_helpers import (
    CLOUD_BASE,
    FakeAttachmentParser,
    FakeConfluenceClient,
    cloud_config,
    full_content,
    light_content,
)
from harborrag_adapters.connectors.confluence import ConfluenceConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


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
    assert document.metadata["children"] == [{"id": "9", "title": "Child", "type": "page"}]
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


def test_load_raises_when_content_filtered_out_by_labels():
    client = FakeConfluenceClient()
    content = full_content()
    content["metadata"] = {"labels": {"results": [{"name": "archived"}]}}
    client.add("content/1", content)
    connector = ConfluenceConnector(cloud_config(exclude_labels=["archived"]), client=client)

    with pytest.raises(DocumentProcessingError, match="does not match label filters"):
        connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))


def test_load_by_ids_loads_each_content_id():
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Page One"), full_content())
    connector = ConfluenceConnector(cloud_config(), client=client)

    documents = list(connector.load_by_ids(["1"]))

    assert [d.id for d in documents] == ["confluence://ENG/1"]
