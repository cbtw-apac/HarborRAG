"""Unit tests for SharePoint connector discovery."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from harborrag_adapters.connectors import SharePointConnector
from harborrag_adapters.connectors.schemas import ConnectorQuery
from sharepoint_test_helpers import (
    FakeGraphClient,
    add_site_and_default_drive,
    config,
    drive,
    file_item,
    folder_item,
    site,
)

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_discover_recurses_pages_and_filters_files():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(
        "drives/drive1/root:/Docs:/children",
        {
            "value": [
                folder_item(),
                file_item("file1", "Guide.docx"),
                file_item("skip", "Draft.txt"),
            ],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/page2",
        },
    )
    client.add(
        "https://graph.microsoft.com/v1.0/page2",
        {"value": [file_item("hidden", "Hidden.docx", hidden=True)]},
    )
    client.add(
        "drives/drive1/items/folder1/children",
        {"value": [file_item("file2", "Nested.pdf", parent_id="folder1")]},
    )
    connector = SharePointConnector(
        config(root_path="Docs", allowed_extensions={".docx", ".pdf"}),
        client=client,
    )

    records = list(
        connector.discover(
            ConnectorQuery(
                updated_after=datetime(2024, 1, 1, tzinfo=UTC),
                pattern="*d*",
            )
        )
    )

    assert [record.metadata["item_id"] for record in records] == ["file1", "file2"]
    assert records[0].id == "sharepoint://site1/drive1/file1"
    assert records[0].source_type.endswith("wordprocessingml.document")
    assert records[0].checksum == "hash-file1"
    assert ("https://graph.microsoft.com/v1.0/page2", None) in client.calls


def test_discover_supports_direct_item_ids_and_named_drives():
    client = FakeGraphClient()
    client.add("sites/contoso.sharepoint.com:/sites/Engineering", site())
    client.add(
        "sites/site1/drives",
        {"value": [drive("drive0", "Assets"), drive("drive1", "Documents")]},
    )
    client.add("drives/drive1/items/file1", file_item("file1", "Guide.docx"))
    connector = SharePointConnector(config(drive_name="Documents"), client=client)

    records = list(connector.discover(ConnectorQuery(filters={"item_ids": ["file1"]})))

    assert records[0].locator == "file1"
    assert records[0].metadata["drive_id"] == "drive1"


def test_discover_stops_at_limit_during_item_id_iteration():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/file1", file_item("file1", "A.docx"))
    client.add("drives/drive1/items/file2", file_item("file2", "B.docx"))
    connector = SharePointConnector(config(), client=client)

    records = list(
        connector.discover(ConnectorQuery(limit=1, filters={"item_ids": ["file1", "file2"]}))
    )

    assert [r.metadata["item_id"] for r in records] == ["file1"]


def test_discover_stops_at_limit_during_folder_walk():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(
        "drives/drive1/root/children",
        {"value": [file_item("file1", "A.docx"), file_item("file2", "B.docx")]},
    )
    connector = SharePointConnector(config(), client=client)

    records = list(connector.discover(ConnectorQuery(limit=1)))

    assert [r.metadata["item_id"] for r in records] == ["file1"]


def test_item_ids_from_query_accepts_bare_string():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/file1", file_item("file1", "A.docx"))
    connector = SharePointConnector(config(), client=client)

    records = list(connector.discover(ConnectorQuery(filters={"item_ids": "file1"})))
    assert [r.metadata["item_id"] for r in records] == ["file1"]


def test_item_ids_from_query_accepts_list_alias():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/file1", file_item("file1", "A.docx"))
    connector = SharePointConnector(config(), client=client)

    records = list(connector.discover(ConnectorQuery(filters={"drive_item_ids": ["file1"]})))
    assert [r.metadata["item_id"] for r in records] == ["file1"]


def test_discover_uses_root_item_id_filter_alias():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(
        "drives/drive1/items/folder1/children",
        {"value": [file_item("file1", "A.docx", parent_id="folder1")]},
    )
    connector = SharePointConnector(config(), client=client)

    records = list(connector.discover(ConnectorQuery(filters={"folder_item_id": "folder1"})))
    assert [r.metadata["item_id"] for r in records] == ["file1"]
