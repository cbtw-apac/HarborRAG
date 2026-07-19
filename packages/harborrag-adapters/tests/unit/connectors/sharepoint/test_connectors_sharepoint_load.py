"""Unit tests for SharePoint connector document loading."""

from __future__ import annotations

import pytest
from harborrag_adapters.connectors import SharePointConnector, SharePointSiteConfig
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_core.domain.source import SourceRecord
from sharepoint_test_helpers import (
    SITE_URL,
    FakeGraphClient,
    add_site_and_default_drive,
    config,
    file_item,
    folder_item,
)

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_load_downloads_file_content_and_builds_metadata():
    client = FakeGraphClient()
    client.add("drives/drive1/items/file1", file_item("file1", "Guide.docx"))
    client.downloads["drives/drive1/items/file1/content"] = b"docx-bytes"
    connector = SharePointConnector(
        SharePointSiteConfig(
            site_id="site1",
            drive_id="drive1",
            access_token="token",
            requests_per_minute=6000,
        ),
        client=client,
    )

    document = connector.load(
        SourceRecord(
            "sharepoint://site1/drive1/file1",
            "application/octet-stream",
            "file1",
            metadata={"drive_id": "drive1", "item_id": "file1"},
        )
    )

    assert document.id == "sharepoint://site1/drive1/file1"
    assert document.content == b"docx-bytes"
    assert document.content_type.endswith("wordprocessingml.document")
    assert document.metadata["source_system"] == "sharepoint"
    assert document.metadata["item_name"] == "Guide.docx"
    assert document.metadata["created_by"] == "Ada"
    assert document.source == f"{SITE_URL}/Shared%20Documents/Guide.docx"
    assert "web_url" not in document.metadata
    assert "site_web_url" not in document.metadata
    assert "drive_web_url" not in document.metadata
    assert "mime_type" not in document.metadata
    assert client.byte_calls == ["drives/drive1/items/file1/content"]


def test_load_rejects_oversized_files_before_download():
    client = FakeGraphClient()
    client.add("drives/drive1/items/file1", file_item("file1", "Guide.docx", size=99))
    connector = SharePointConnector(
        SharePointSiteConfig(
            site_id="site1",
            drive_id="drive1",
            access_token="token",
            max_file_size_bytes=10,
            requests_per_minute=6000,
        ),
        client=client,
    )

    with pytest.raises(DocumentProcessingError, match="max_file_size_bytes"):
        connector.load(
            SourceRecord(
                "sharepoint://site1/drive1/file1",
                "application/octet-stream",
                "file1",
                metadata={"drive_id": "drive1", "item_id": "file1"},
            )
        )
    assert client.byte_calls == []


def test_load_rejects_folders():
    client = FakeGraphClient()
    client.add("drives/drive1/items/folder1", folder_item())
    connector = SharePointConnector(
        SharePointSiteConfig(
            site_id="site1",
            drive_id="drive1",
            access_token="token",
            requests_per_minute=6000,
        ),
        client=client,
    )

    with pytest.raises(DocumentProcessingError, match="not a downloadable file"):
        connector.load(SourceRecord("sharepoint://site1/drive1/folder1", "folder", "folder1"))


def test_load_by_ids_loads_each_item():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    # record_for_item_id() fetches the item once to build the record, then
    # load() fetches it again to confirm it's still a file before downloading.
    client.add(
        "drives/drive1/items/file1",
        file_item("file1", "A.docx"),
        file_item("file1", "A.docx"),
    )
    client.downloads["drives/drive1/items/file1/content"] = b"bytes-a"
    connector = SharePointConnector(config(), client=client)

    documents = list(connector.load_by_ids(["file1"]))

    assert [d.content for d in documents] == [b"bytes-a"]
