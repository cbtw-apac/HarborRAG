from __future__ import annotations

from datetime import datetime
from typing import Any

from harborrag_core.domain.source import SourceRecord

from .schemas import SharePointMetadata, SharePointParentReference
from .utils import item_mime_type, item_name, item_path


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse Microsoft Graph timestamp strings into datetimes."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def drive_item_id_from_record(record: SourceRecord) -> str:
    """Recover the Graph drive item ID from a source record."""
    item_id = record.metadata.get("item_id") or record.locator
    if not item_id:
        raise ValueError(f"SourceRecord {record.id!r} does not contain item_id")
    return str(item_id).rstrip("/").rsplit("/", 1)[-1]


def build_source_record(
    item: dict[str, Any],
    *,
    site_id: str,
    drive_id: str,
) -> SourceRecord:
    """Convert a Microsoft Graph drive item into a source record."""
    item_id = str(item.get("id") or "")
    mime_type = item_mime_type(item)
    path = item_path(item)
    checksum = _item_checksum(item)

    return SourceRecord(
        id=f"sharepoint://{site_id}/{drive_id}/{item_id}",
        source_type=mime_type,
        locator=item_id,
        updated_at=parse_timestamp(item.get("lastModifiedDateTime")),
        checksum=checksum,
        metadata={
            "source_system": "sharepoint",
            "site_id": site_id,
            "drive_id": drive_id,
            "item_id": item_id,
            "name": item_name(item),
            "path": path,
            "size": int(item.get("size") or 0),
            "etag": item.get("eTag"),
            "ctag": item.get("cTag"),
            "created_at": parse_timestamp(item.get("createdDateTime")),
            "created_by": _identity_name(item.get("createdBy")),
            "updated_by": _identity_name(item.get("lastModifiedBy")),
            "parent_id": item.get("parentReference", {}).get("id"),
            "parent_path": item.get("parentReference", {}).get("path"),
        },
    )


def build_document_metadata(
    item: dict[str, Any],
    *,
    site: dict[str, Any],
    drive: dict[str, Any],
    checksum: str,
) -> SharePointMetadata:
    """Build parsed provenance metadata for a loaded SharePoint file."""
    parent = item.get("parentReference", {})
    return SharePointMetadata(
        source_system="sharepoint",
        site_id=site.get("id"),
        site_name=site.get("name") or site.get("displayName"),
        drive_id=drive.get("id"),
        drive_name=drive.get("name"),
        drive_type=drive.get("driveType"),
        item_id=item.get("id"),
        item_name=item_name(item),
        path=item_path(item),
        size=int(item.get("size") or 0),
        checksum=checksum,
        etag=item.get("eTag"),
        ctag=item.get("cTag"),
        created_at=parse_timestamp(item.get("createdDateTime")),
        updated_at=parse_timestamp(item.get("lastModifiedDateTime")),
        created_by=_identity_name(item.get("createdBy")),
        updated_by=_identity_name(item.get("lastModifiedBy")),
        parent=SharePointParentReference(
            drive_id=parent.get("driveId"),
            id=parent.get("id"),
            path=parent.get("path"),
        ),
        sharepoint_hashes=_hashes(item),
    )


def _item_checksum(item: dict[str, Any]) -> str | None:
    hashes = _hashes(item)
    return (
        hashes.get("quickXorHash")
        or hashes.get("sha1Hash")
        or hashes.get("sha256Hash")
        or item.get("eTag")
        or item.get("cTag")
    )


def _hashes(item: dict[str, Any]) -> dict[str, Any]:
    file_info = item.get("file")
    if not isinstance(file_info, dict):
        return {}
    hashes = file_info.get("hashes")
    return hashes if isinstance(hashes, dict) else {}


def _identity_name(identity_set: Any) -> str | None:
    if not isinstance(identity_set, dict):
        return None
    for value in identity_set.values():
        if isinstance(value, dict):
            display_name = value.get("displayName") or value.get("email")
            if display_name:
                return str(display_name)
    return None
