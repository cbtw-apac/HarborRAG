"""SharePoint query paths, item IDs and loads stay inside config.root_path."""

from __future__ import annotations

import pytest
from sharepoint_test_helpers import (
    FakeGraphClient,
    add_site_and_default_drive,
    config,
    file_item,
    folder_item,
)

from harborrag_adapters.connectors import SharePointConnector
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_adapters.connectors.sharepoint.drive_paths import (
    item_within_root,
    normalize_drive_path,
    scoped_drive_root,
)
from harborrag_core.domain.source import SourceRecord

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def private_file(item_id: str = "secret") -> dict:
    item = file_item(item_id, "Payroll.xlsx")
    item["parentReference"]["path"] = "/drive/root:/Private"
    return item


def test_normalize_drive_path_rejects_parent_segments():
    with pytest.raises(ValueError, match=r"'\.\.'"):
        normalize_drive_path("Docs/../Private")


def test_scoped_drive_root_cannot_widen_the_configured_root():
    assert scoped_drive_root("Docs", None) == "Docs"
    assert scoped_drive_root("Docs", "Docs/Runbooks") == "Docs/Runbooks"
    assert scoped_drive_root(None, "Private") == "Private"
    with pytest.raises(ValueError, match="outside configured root_path"):
        scoped_drive_root("Docs", "Private")


def test_scoped_drive_root_rejects_prefix_siblings_and_accepts_case_and_encoding():
    with pytest.raises(ValueError, match="outside configured root_path"):
        scoped_drive_root("Docs", "Docs-secret")
    assert scoped_drive_root("Docs", "DOCS/Runbooks") == "DOCS/Runbooks"
    assert scoped_drive_root("Shared Docs", "Shared%20Docs/Runbooks") == "Shared%20Docs/Runbooks"


def test_item_within_root_normalises_encoding_and_case():
    encoded = file_item()
    encoded["parentReference"]["path"] = "/drive/root:/Shared%20Docs"
    no_root_marker = file_item()
    no_root_marker["parentReference"] = {"id": "x"}
    sibling = file_item()
    sibling["parentReference"]["path"] = "/drive/root:/Docs-secret"

    assert item_within_root(file_item(), "Docs")
    assert item_within_root(file_item(), "docs")
    assert item_within_root(encoded, "Shared Docs")
    assert not item_within_root(private_file(), "Docs")
    assert not item_within_root(sibling, "Docs")
    assert not item_within_root(no_root_marker, "Docs")
    assert item_within_root(private_file(), None)


def test_discover_rejects_query_path_outside_root():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    connector = SharePointConnector(config(root_path="Docs"), client=client)

    with pytest.raises(ValueError, match="outside configured root_path"):
        list(connector.discover(ConnectorQuery(path="Private")))
    assert not [call for call in client.calls if "children" in call[0]]


def test_discover_rejects_query_path_with_parent_segments_before_any_listing():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    connector = SharePointConnector(config(root_path="Docs"), client=client)

    with pytest.raises(ValueError, match=r"'\.\.'"):
        list(connector.discover(ConnectorQuery(path="Docs/../Private")))
    assert not [call for call in client.calls if "children" in call[0]]


def test_discover_rejects_item_ids_outside_root():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/secret", private_file())
    connector = SharePointConnector(config(root_path="Docs"), client=client)

    with pytest.raises(DocumentProcessingError, match="outside configured root_path"):
        list(connector.discover(ConnectorQuery(filters={"item_ids": ["secret"]})))


def test_discover_rejects_root_item_id_outside_root():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/folder-x", folder_item("folder-x", "Private"))
    connector = SharePointConnector(config(root_path="Docs"), client=client)

    with pytest.raises(DocumentProcessingError, match="outside configured root_path"):
        list(connector.discover(ConnectorQuery(filters={"root_item_id": "folder-x"})))
    assert not [call for call in client.calls if "children" in call[0]]


def test_walk_skips_remote_children_outside_root_without_aborting():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add(
        "drives/drive1/root:/Docs:/children",
        {"value": [private_file("leak"), file_item("ok", "Guide.docx")]},
    )
    connector = SharePointConnector(config(root_path="Docs"), client=client)

    records = list(connector.discover(ConnectorQuery()))

    assert [record.metadata["item_id"] for record in records] == ["ok"]


def test_load_rejects_item_outside_root_before_download():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/secret", private_file())
    connector = SharePointConnector(config(root_path="Docs"), client=client)

    with pytest.raises(DocumentProcessingError, match="outside configured root_path"):
        connector.load(
            SourceRecord(
                "sharepoint://site1/drive1/secret",
                "application/octet-stream",
                "secret",
                metadata={"drive_id": "drive1", "item_id": "secret"},
            )
        )
    assert client.byte_calls == []
    assert not [call for call in client.calls if call[0].endswith("/permissions")]
