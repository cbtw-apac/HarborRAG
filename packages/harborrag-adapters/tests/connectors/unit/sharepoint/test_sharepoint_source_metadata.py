from __future__ import annotations

import pytest

from harborrag_adapters.connectors.sharepoint.mappers import (
    build_document_metadata,
    build_page_document_metadata,
)

pytestmark = pytest.mark.unit

SITE = {
    "id": "contoso.sharepoint.com,SITE-GUID,WEB-GUID",
    "name": "hr",
    "webUrl": "https://contoso.sharepoint.com/sites/hr",
}
DRIVE = {"id": "DRIVE-1", "name": "Documents", "driveType": "documentLibrary"}
ITEM = {
    "id": "ITEM-7",
    "name": "Policy.docx",
    "webUrl": "https://contoso.sharepoint.com/sites/hr/Shared%20Documents/Policy.docx",
    "size": 2048,
    "eTag": "etag-1",
    "cTag": "ctag-1",
    "createdDateTime": "2026-04-01T09:00:00Z",
    "lastModifiedDateTime": "2026-05-01T08:30:00+02:00",
    "createdBy": {"user": {"displayName": "Alex Example"}},
    "lastModifiedBy": {"user": {"displayName": "Sam Sample"}},
    "parentReference": {"driveId": "DRIVE-1", "id": "PARENT", "path": "/drives/DRIVE-1/root:"},
    "file": {"mimeType": "application/octet-stream", "hashes": {"quickXorHash": "h"}},
}
PAGE = {
    "id": "PAGE-3",
    "name": "Welcome.aspx",
    "title": "Welcome",
    "webUrl": "https://contoso.sharepoint.com/sites/hr/SitePages/Welcome.aspx",
    "createdDateTime": "2026-04-02T09:00:00Z",
    "lastModifiedDateTime": "2026-05-03T10:15:00Z",
    "eTag": "etag-page",
    "createdBy": {"user": {"displayName": "Alex Example"}},
}


def test_file_metadata_carries_url_identity_author_and_utc_times() -> None:
    """A drive file's metadata has its link, IDs, author and UTC times."""
    metadata = build_document_metadata(ITEM, site=SITE, drive=DRIVE, checksum="sum").to_dict()

    assert metadata["url"] == ITEM["webUrl"]
    assert metadata["site_id"] == SITE["id"]
    assert metadata["drive_id"] == "DRIVE-1"
    assert metadata["item_id"] == "ITEM-7"
    assert metadata["author"] == "Alex Example"
    assert metadata["created_at"] == "2026-04-01T09:00:00+00:00"
    assert metadata["updated_at"] == "2026-05-01T06:30:00+00:00"


def test_site_page_metadata_carries_its_url() -> None:
    """A site page's metadata has its link and page ID."""
    metadata = build_page_document_metadata(
        PAGE, site=SITE, title="Welcome", checksum="sum", has_text_content=True
    ).to_dict()

    assert metadata["url"] == PAGE["webUrl"]
    assert metadata["page_id"] == "PAGE-3"


def test_file_without_web_url_has_no_url() -> None:
    """A drive item without a web URL gets no URL."""
    item = {key: value for key, value in ITEM.items() if key != "webUrl"}

    metadata = build_document_metadata(item, site=SITE, drive=DRIVE, checksum="sum").to_dict()

    assert metadata["url"] is None
