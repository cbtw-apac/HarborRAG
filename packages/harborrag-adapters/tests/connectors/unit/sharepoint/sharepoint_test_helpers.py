"""Shared fake client and fixture builders for SharePoint connector tests."""

from __future__ import annotations

from typing import Any

from harborrag_adapters.connectors import SharePointSiteConfig

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
            "mimeType": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
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
