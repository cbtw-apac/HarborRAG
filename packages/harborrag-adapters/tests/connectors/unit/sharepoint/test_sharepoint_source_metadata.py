from __future__ import annotations

from harborrag_adapters.connectors.sharepoint.mappers import build_document_metadata

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
    "createdBy": {"user": {"displayName": "Ada Lovelace"}},
    "lastModifiedBy": {"user": {"displayName": "Grace Hopper"}},
    "parentReference": {"driveId": "DRIVE-1", "id": "PARENT", "path": "/drives/DRIVE-1/root:"},
    "file": {"mimeType": "application/octet-stream", "hashes": {"quickXorHash": "h"}},
}


def test_file_metadata_carries_source_url_identity_author_and_utc_timestamp() -> None:
    metadata = build_document_metadata(ITEM, site=SITE, drive=DRIVE, checksum="sum").to_dict()

    assert metadata["url"] == ITEM["webUrl"]
    assert metadata["site_id"] == SITE["id"]
    assert metadata["drive_id"] == "DRIVE-1"
    assert metadata["item_id"] == "ITEM-7"
    assert metadata["author"] == "Ada Lovelace"
    # 08:30 at +02:00 is 06:30 UTC; the stored value is normalized to UTC.
    assert metadata["updated_at"] == "2026-05-01T06:30:00+00:00"
