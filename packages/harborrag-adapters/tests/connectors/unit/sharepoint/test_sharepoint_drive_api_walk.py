"""Whitebox unit tests for SharePointDriveAPI child-walk and item resolution."""

from __future__ import annotations

import pytest
from sharepoint_test_helpers import (
    FakeGraphClient,
    add_site_and_default_drive,
    config,
    drive,
    file_item,
    folder_item,
    site,
)

from harborrag_adapters.connectors.exceptions import FetchError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_adapters.connectors.sharepoint.drive import SharePointDriveAPI

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_walk_children_stops_descending_when_not_recursive():
    client = FakeGraphClient()
    client.add(
        "drives/drive1/root/children",
        {"value": [folder_item(), file_item("file1", "Guide.docx")]},
    )
    api = SharePointDriveAPI(client, config())

    records = list(
        api.walk_children(site=site(), drive=drive(), query=ConnectorQuery(recursive=False))
    )

    assert [r.metadata["item_id"] for r in records] == ["file1"]
    assert "drives/drive1/items/folder1/children" not in [c[0] for c in client.calls]


def test_walk_children_handles_deeply_nested_folders_without_recursion_error():
    client = FakeGraphClient()
    depth = 3000
    client.add(
        "drives/drive1/root/children",
        {"value": [folder_item("folder-0", "level-0")]},
    )
    for level in range(depth):
        endpoint = f"drives/drive1/items/folder-{level}/children"
        if level == depth - 1:
            child: dict = file_item("leaf", "leaf.txt")
        else:
            child = folder_item(f"folder-{level + 1}", f"level-{level + 1}")
        client.add(endpoint, {"value": [child]})
    api = SharePointDriveAPI(client, config())

    records = list(
        api.walk_children(site=site(), drive=drive(), query=ConnectorQuery(recursive=True))
    )

    assert [r.metadata["item_id"] for r in records] == ["leaf"]


def test_records_from_item_id_file_filtered_out_yields_nothing():
    client = FakeGraphClient()
    client.add("drives/drive1/items/file1", file_item("file1", "Draft.tmp"))
    api = SharePointDriveAPI(client, config(excluded_extensions={".tmp"}))

    records = list(
        api.records_from_item_id("file1", site=site(), drive=drive(), query=ConnectorQuery())
    )
    assert records == []


def test_walk_children_skips_folder_without_id():
    client = FakeGraphClient()
    client.add(
        "drives/drive1/root/children",
        {"value": [{"folder": {"childCount": 0}, "parentReference": {}}]},
    )
    api = SharePointDriveAPI(client, config())

    records = list(api.walk_children(site=site(), drive=drive(), query=ConnectorQuery()))
    assert records == []
    # No recursive call for the id-less folder means only the root page was fetched.
    assert len(client.calls) == 1


def test_walk_children_rejects_folder_cycles():
    client = FakeGraphClient()
    client.add(
        "drives/drive1/root/children",
        {"value": [folder_item("folder1")]},
    )
    client.add(
        "drives/drive1/items/folder1/children",
        {"value": [folder_item("folder1")]},
    )
    api = SharePointDriveAPI(client, config())

    with pytest.raises(FetchError, match="contains a cycle"):
        list(api.walk_children(site=site(), drive=drive(), query=ConnectorQuery()))


def test_iter_children_rejects_repeated_next_link():
    endpoint = "drives/drive1/root/children"
    client = FakeGraphClient()
    client.add(endpoint, {"value": [], "@odata.nextLink": endpoint})
    api = SharePointDriveAPI(client, config())

    with pytest.raises(FetchError, match="did not advance"):
        list(api.iter_children("drive1"))


def test_iter_children_rejects_invalid_page_shape():
    client = FakeGraphClient()
    client.add("drives/drive1/root/children", {"value": "not-a-list"})
    api = SharePointDriveAPI(client, config())

    with pytest.raises(FetchError, match="invalid value list"):
        list(api.iter_children("drive1"))


def test_records_from_item_id_for_file_and_folder_and_neither():
    client = FakeGraphClient()
    client.add("drives/drive1/items/file1", file_item("file1", "Guide.docx"))
    client.add("drives/drive1/items/folder1", folder_item())
    client.add("drives/drive1/items/folder1/children", {"value": []})
    client.add("drives/drive1/items/other1", {"id": "other1", "name": "weird"})
    api = SharePointDriveAPI(client, config())

    file_records = list(
        api.records_from_item_id("file1", site=site(), drive=drive(), query=ConnectorQuery())
    )
    assert [r.metadata["item_id"] for r in file_records] == ["file1"]

    folder_records = list(
        api.records_from_item_id("folder1", site=site(), drive=drive(), query=ConnectorQuery())
    )
    assert folder_records == []

    neither_records = list(
        api.records_from_item_id("other1", site=site(), drive=drive(), query=ConnectorQuery())
    )
    assert neither_records == []


def test_resolve_drive_by_name_not_found_raises():
    client = FakeGraphClient()
    client.add("sites/site1/drives", {"value": [drive("drive0", "Assets")]})
    api = SharePointDriveAPI(client, config(drive_name="Missing"))

    with pytest.raises(Exception, match="was not found"):
        api.resolve_drive(site())


def test_resolve_drive_default_missing_id_raises():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = FakeGraphClient()
    client.add("sites/site1/drive", {})
    api = SharePointDriveAPI(client, config())

    with pytest.raises(FetchError, match="did not include id"):
        api.resolve_drive(site())


def test_resolve_site_missing_id_raises():
    from harborrag_adapters.connectors.exceptions import FetchError

    client = FakeGraphClient()
    client.add("sites/contoso.sharepoint.com:/sites/Engineering", {})
    api = SharePointDriveAPI(client, config())

    with pytest.raises(FetchError, match="did not include id"):
        api.resolve_site()


def test_record_for_item_id_resolves_site_and_drive():
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    client.add("drives/drive1/items/file1", file_item("file1", "Guide.docx"))
    api = SharePointDriveAPI(client, config())

    record = api.record_for_item_id("file1")
    assert record.metadata["item_id"] == "file1"


def test_iter_site_drives_follows_pagination():
    client = FakeGraphClient()
    client.add(
        "sites/site1/drives",
        {
            "value": [drive("drive0", "Assets")],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/drives-page2",
        },
    )
    client.add(
        "https://graph.microsoft.com/v1.0/drives-page2",
        {"value": [drive("drive1", "Documents")]},
    )
    api = SharePointDriveAPI(client, config())

    drives = list(api.iter_site_drives("site1"))
    assert [d["id"] for d in drives] == ["drive0", "drive1"]


def test_iter_site_drives_rejects_repeated_next_link():
    endpoint = "sites/site1/drives"
    client = FakeGraphClient()
    client.add(endpoint, {"value": [], "@odata.nextLink": endpoint})
    api = SharePointDriveAPI(client, config())

    with pytest.raises(FetchError, match="did not advance"):
        list(api.iter_site_drives("site1"))
