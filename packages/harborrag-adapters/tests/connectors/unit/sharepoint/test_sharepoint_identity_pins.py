"""SharePoint output keeps its document identity and change-detection values."""

from __future__ import annotations

import pytest
from sharepoint_test_helpers import (
    FakeGraphClient,
    add_site_and_default_drive,
    config,
    file_item,
    fixture,
)

from harborrag_adapters.connectors.sharepoint import SharePointConnector
from harborrag_core.chunking import ConnectorType

pytestmark = pytest.mark.unit

PINS: dict[str, tuple[str, str, str, str]] = {
    "sharepoint://site1/drive1/file1": (
        "document:1b4395c5d3a19e8b5d9bd3973b06b6d2d60f36c5a397c45dfbdff3c87260cc9e",
        "admission-change:9baab2224ac5f948a209771697207546d76e267c80eb71bf915a7af750dbf8e8",
        "e6ffe813059b6dbbfac11ae896e2afa922c9c91d6f9755d61c6e0b4da2db331e",
        "retrieval-metadata:808785af073b389b577fc61988bb04885fbb785fcbc2922449da5db243cfb9ad",
    ),
    "sharepoint://site1/pages/page1": (
        "document:313ee7f63da9ab3b23abc4f2c80b51ba968a63d5815ba73f0ee046470fe25989",
        "admission-change:53ff4f873499294e0e4afedd8f35d9080a0dec46500af422c810d06963915a88",
        "cd101fd176468cf6c24e7d049d7d1e9af69e258036a3430d5b75e8dbaecdf043",
        "retrieval-metadata:95f516b02fdaa81b00bc07ea0f7c04872391b08c898b060344e08b936da2033f",
    ),
}


def test_sharepoint_page_and_file_keep_their_identity_and_hashes(pin_connector) -> None:
    client = FakeGraphClient()
    add_site_and_default_drive(client)
    pages = fixture("pages_list.json")
    pages["value"] = pages["value"][:1]
    client.add("sites/site1/pages/microsoft.graph.sitePage", pages)
    guide = file_item("file1", "Guide.md")
    guide["file"]["mimeType"] = "text/markdown"
    client.add("drives/drive1/root/children", {"value": [guide]})
    for _ in range(3):
        add_site_and_default_drive(client)
        client.add("sites/site1/pages/page1/microsoft.graph.sitePage", fixture("page_canvas.json"))
        client.add("drives/drive1/items/file1", guide)
    client.downloads["drives/drive1/items/file1/content"] = b"# Guide\n\nRequest access first.\n"
    connector = SharePointConnector(config(include_pages=True), client=client)

    assert pin_connector(connector, connector_type=ConnectorType("sharepoint")) == PINS
