"""Mappings from Microsoft Graph payloads to source records and metadata.

Handles:
- Source records and document metadata
- Timestamps, checksums and identities
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from harborrag_core.domain.source import SourceRecord

from .drive_paths import item_mime_type, item_name, item_path
from .schemas import SharePointMetadata, SharePointParentReference


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse a Graph ISO timestamp. Returns ``None`` if empty or invalid."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def drive_item_id_from_record(record: SourceRecord) -> str:
    """Return the drive item id from a record's metadata or locator.

    Raises:
        ValueError: If the record has no item id.
    """
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
    """Build the source record for a drive item.

    Args:
        item: Graph drive item.
        site_id: Id of the site the item belongs to.
        drive_id: Id of the drive the item belongs to.
    """
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
    """Build the metadata for a loaded drive file.

    Args:
        item: Graph drive item.
        site: Resolved site payload.
        drive: Resolved drive payload.
        checksum: SHA-256 hex digest of the downloaded bytes.
    """
    parent = item.get("parentReference", {})
    item_id = str(item.get("id") or "")
    title = item_name(item)
    return SharePointMetadata(
        record_id=item_id,
        title=title,
        checksum=checksum,
        created_at=parse_timestamp(item.get("createdDateTime")),
        updated_at=parse_timestamp(item.get("lastModifiedDateTime")),
        site_id=site.get("id"),
        site_name=site.get("name") or site.get("displayName"),
        drive_id=drive.get("id"),
        drive_name=drive.get("name"),
        drive_type=drive.get("driveType"),
        item_id=item_id,
        item_name=title,
        path=item_path(item),
        size=int(item.get("size") or 0),
        etag=item.get("eTag"),
        ctag=item.get("cTag"),
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
    """Return the quickXor, SHA-1 or SHA-256 hash, else ``eTag``, else ``cTag``."""
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
    """Return the first display name or email in a Graph identity set, or ``None``."""
    if not isinstance(identity_set, dict):
        return None
    for value in identity_set.values():
        if isinstance(value, dict):
            display_name = value.get("displayName") or value.get("email")
            if display_name:
                return str(display_name)
    return None
