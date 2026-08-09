"""Unit tests for Confluence connector document loading."""

from __future__ import annotations

import logging

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


def test_load_builds_raw_document_metadata_comments_and_attachments(caplog):
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

    with caplog.at_level(logging.INFO, logger="harborrag.adapters.connectors.confluence"):
        document = connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))

    assert document.id == "confluence://ENG/1"
    assert document.content_type == "text/html"
    assert document.content == "<p>Hello <b>World</b></p>"
    assert document.source == f"{CLOUD_BASE}/spaces/ENG/pages/1"
    assert document.metadata["source_system"] == "confluence"
    assert document.metadata["metadata_schema_version"] == 1
    assert document.metadata["record_id"] == "1"
    assert document.metadata["title"] == "Page One"
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
    assert "Confluence content loaded content_id=1 comments=1 attachments=1" in caplog.text


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


def test_load_truncates_comments_over_configured_limit():
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

    document = connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))

    assert [comment["id"] for comment in document.metadata["comments"]] == ["c1"]


def test_load_truncates_attachments_over_configured_limit():
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
        parser=FakeAttachmentParser(),
    )

    document = connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))

    assert len(document.metadata["attachments"]) == 1


def test_load_truncates_children_metadata_over_configured_limit():
    client = FakeConfluenceClient()
    content = full_content()
    content["children"] = {
        "page": {"results": [{"id": "9", "title": "Child"}, {"id": "10", "title": "Child 2"}]}
    }
    client.add("content/1", content)
    connector = ConfluenceConnector(
        cloud_config(max_child_pages=1),
        client=client,
    )

    document = connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))

    assert document.metadata["children"] == [{"id": "9", "title": "Child", "type": "page"}]


def test_load_respects_record_comment_and_attachment_flags():
    client = FakeConfluenceClient()
    client.add("content/1", full_content())
    connector = ConfluenceConnector(
        cloud_config(include_comments=True, include_attachments=True),
        client=client,
        parser=FakeAttachmentParser(),
    )
    record = SourceRecord(
        "confluence://ENG/1",
        "text/html",
        "1",
        metadata={"include_comments": False, "include_attachments": False},
    )

    document = connector.load(record)

    assert document.metadata["comments"] == []
    assert document.metadata["attachments"] == []
    assert [endpoint for endpoint, _ in client.calls] == ["content/1"]


def test_load_rejects_stale_attachment_record_when_attachments_disabled():
    connector = ConfluenceConnector(
        cloud_config(include_attachments=False),
        client=FakeConfluenceClient(),
    )
    record = SourceRecord(
        "confluence://ENG/1/attachments/a1",
        "text/markdown",
        "a1",
        metadata={"binding_kind": "ATTACHMENT"},
    )

    with pytest.raises(DocumentProcessingError, match="attachment loading is disabled"):
        connector.load(record)


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

    with pytest.raises(DocumentProcessingError, match="does not match content filters"):
        connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))


def test_load_raises_for_live_doc_content_even_when_content_types_is_page():
    # Live docs report type: "page" like ordinary pages -- only "subtype:
    # live" distinguishes them, which content_types (a CQL type filter)
    # cannot see. There's no supported way to opt into ingesting them.
    client = FakeConfluenceClient()
    content = full_content()
    content["subtype"] = "live"
    client.add("content/1", content)
    connector = ConfluenceConnector(cloud_config(content_types=["page"]), client=client)

    with pytest.raises(DocumentProcessingError, match="does not match content filters"):
        connector.load(SourceRecord("confluence://ENG/1", "text/html", "1"))


def test_load_by_ids_loads_each_content_id():
    client = FakeConfluenceClient()
    client.add("content/1", light_content("1", "Page One"), full_content())
    connector = ConfluenceConnector(cloud_config(), client=client)

    documents = list(connector.load_by_ids(["1"]))

    assert [d.id for d in documents] == ["confluence://ENG/1"]
