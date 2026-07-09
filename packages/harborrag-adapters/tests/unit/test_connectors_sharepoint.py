from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from harborrag_adapters.connectors import SharePointConnector, SharePointSiteConfig
from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_adapters.connectors.sharepoint.utils import (
    children_endpoint,
    parse_sharepoint_site_url,
)
from harborrag_core.domain.source import SourceRecord


pytestmark = [pytest.mark.unit, pytest.mark.graybox]


SITE_URL = "https://contoso.sharepoint.com/sites/Engineering"


class FakeGraphClient:
    def __init__(self) -> None:
        self.responses: dict[str, list[dict[str, Any]]] = {}
        self.downloads: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.byte_calls: list[str] = []

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
            raise AssertionError(f"Unexpected Microsoft Graph endpoint: {endpoint}")
        return values.pop(0)

    def get_bytes(self, endpoint: str) -> bytes:
        self.byte_calls.append(endpoint)
        try:
            return self.downloads[endpoint]
        except KeyError as exc:
            raise AssertionError(f"Unexpected Microsoft Graph bytes: {endpoint}") from exc


def config(**overrides: Any) -> SharePointSiteConfig:
    values = {
        "site_url": SITE_URL,
        "access_token": "token",
        "requests_per_minute": 6000,
        "page_size": 2,
    }
    values.update(overrides)
    return SharePointSiteConfig(**values)


def site() -> dict[str, Any]:
    return {
        "id": "site1",
        "name": "Engineering",
        "displayName": "Engineering",
        "webUrl": SITE_URL,
    }


def drive(drive_id: str = "drive1", name: str = "Documents") -> dict[str, Any]:
    return {
        "id": drive_id,
        "name": name,
        "driveType": "documentLibrary",
        "webUrl": f"{SITE_URL}/{name}",
    }


def file_item(
    item_id: str = "file1",
    name: str = "Guide.docx",
    *,
    parent_id: str = "root",
    size: int = 12,
    updated: str = "2024-05-24T20:57:56Z",
    hidden: bool = False,
) -> dict[str, Any]:
    item = {
        "id": item_id,
        "name": name,
        "webUrl": f"{SITE_URL}/Shared%20Documents/{name}",
        "size": size,
        "file": {
            "mimeType": (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            "hashes": {"quickXorHash": f"hash-{item_id}"},
        },
        "parentReference": {
            "driveId": "drive1",
            "id": parent_id,
            "path": "/drive/root:/Docs",
        },
        "createdDateTime": "2024-01-01T00:00:00Z",
        "lastModifiedDateTime": updated,
        "createdBy": {"user": {"displayName": "Ada"}},
        "lastModifiedBy": {"user": {"displayName": "Grace"}},
        "eTag": f"etag-{item_id}",
        "cTag": f"ctag-{item_id}",
    }
    if hidden:
        item["hidden"] = {}
    return item


def folder_item(item_id: str = "folder1", name: str = "Runbooks") -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "folder": {"childCount": 1},
        "parentReference": {"driveId": "drive1", "id": "root", "path": "/drive/root:"},
    }


def add_site_and_default_drive(client: FakeGraphClient) -> None:
    client.add("sites/contoso.sharepoint.com:/sites/Engineering", site())
    client.add("sites/site1/drive", drive())


def test_config_parses_site_url_and_normalizes_extensions():
    cfg = config(allowed_extensions={"docx", ".PDF"}, excluded_extensions={"tmp"})

    assert cfg.hostname == "contoso.sharepoint.com"
    assert cfg.site_path == "sites/Engineering"
    assert cfg.allowed_extensions == {".docx", ".pdf"}
    assert cfg.excluded_extensions == {".tmp"}
    assert parse_sharepoint_site_url(SITE_URL) == (
        "contoso.sharepoint.com",
        "sites/Engineering",
    )


def test_children_endpoint_supports_root_path_and_item_children():
    assert children_endpoint("drive1") == "drives/drive1/root/children"
    assert (
        children_endpoint("drive1", path="Shared Documents")
        == "drives/drive1/root:/Shared%20Documents:/children"
    )
    assert (
        children_endpoint("drive1", item_id="folder1")
        == "drives/drive1/items/folder1/children"
    )


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

    records = list(
        connector.discover(ConnectorQuery(filters={"item_ids": ["file1"]}))
    )

    assert records[0].locator == "file1"
    assert records[0].metadata["drive_id"] == "drive1"


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
        connector.load(
            SourceRecord("sharepoint://site1/drive1/folder1", "folder", "folder1")
        )
