from __future__ import annotations

from harborrag_core.domain.source import SourceRecord

from .sources import AttachmentSourceDescriptor

_ATTACHMENT_BINDING = "ATTACHMENT"


def attachment_source_record(
    parent: SourceRecord,
    descriptor: AttachmentSourceDescriptor,
) -> SourceRecord:
    """Create a stable, independently dispatchable attachment source record."""

    return SourceRecord(
        id=f"{parent.id}/attachments/{descriptor.attachment_id}",
        source_type=descriptor.media_type,
        locator=descriptor.attachment_id,
        metadata={
            "binding_kind": _ATTACHMENT_BINDING,
            "parent_source_item_id": parent.id,
            "parent_source_url": parent.metadata.get("url"),
            "attachment_id": descriptor.attachment_id,
            "title": descriptor.title,
            "filename": descriptor.title,
            "media_type": descriptor.media_type,
            "size_bytes": descriptor.size_bytes,
            "download_url": descriptor.download_url,
            "source_version": descriptor.source_version,
            "attachment_status": descriptor.status,
        },
    )


def attachment_descriptor_from_record(
    record: SourceRecord,
) -> AttachmentSourceDescriptor:
    metadata = record.metadata
    if metadata.get("binding_kind") != _ATTACHMENT_BINDING:
        raise ValueError("source record is not an attachment binding")
    return AttachmentSourceDescriptor(
        attachment_id=str(metadata.get("attachment_id") or record.locator),
        title=str(metadata.get("filename") or metadata.get("title") or ""),
        media_type=str(metadata.get("media_type") or record.source_type),
        size_bytes=int(metadata.get("size_bytes") or 0),
        download_url=str(metadata.get("download_url") or ""),
        source_version=str(metadata.get("source_version") or ""),
        status="admitted",
    )


def is_attachment_record(record: SourceRecord) -> bool:
    return record.metadata.get("binding_kind") == _ATTACHMENT_BINDING
