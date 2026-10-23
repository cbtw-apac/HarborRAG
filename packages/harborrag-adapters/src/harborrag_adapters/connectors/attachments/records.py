"""Source records for attachments dispatched as their own items.

Handles:
- Binding records (``binding_kind`` ``ATTACHMENT``) carrying the descriptor fields
- Rebuilding a descriptor from a record
- Resolving the parent locator of an attachment

Does NOT check the parent's access; connectors re-check it before loading.
"""

from __future__ import annotations

from collections.abc import Mapping

from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_core.domain.source import SourceRecord

from .sources import AttachmentSourceDescriptor

_ATTACHMENT_BINDING = "ATTACHMENT"

CONTAINER_IDENTITY_KEYS = ("space_id", "space_key", "project_id", "project_key")


def attachment_source_record(
    parent: SourceRecord,
    descriptor: AttachmentSourceDescriptor,
    *,
    inherited: Mapping[str, object] | None = None,
) -> SourceRecord:
    """Build the independently dispatchable source record of one attachment.

    The record id is ``<parent id>/attachments/<attachment id>``.

    Args:
        parent: Record of the issue or page the attachment belongs to.
        descriptor: Admitted attachment descriptor.
        inherited: Parent's container identity and ACL metadata (space or project keys).

    Returns:
        Binding record that ``attachment_descriptor_from_record`` can read back.
    """

    return SourceRecord(
        id=f"{parent.id}/attachments/{descriptor.attachment_id}",
        source_type=descriptor.media_type,
        locator=descriptor.attachment_id,
        metadata={
            **dict(inherited or {}),
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
    """Rebuild the ``admitted`` descriptor stored on an attachment binding record.

    Args:
        record: Attachment binding record.

    Returns:
        Descriptor read from the record metadata.

    Raises:
        ValueError: If the record is not an attachment binding.
    """

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
    """Return whether the record is an attachment binding."""
    return record.metadata.get("binding_kind") == _ATTACHMENT_BINDING


def attachment_parent_locator(record: SourceRecord) -> str:
    """Return the provider locator of an attachment's parent (issue key, page id).

    Args:
        record: Attachment binding record whose parent id ends in the locator.

    Returns:
        Locator taken from the last segment of ``parent_source_item_id``.

    Raises:
        DocumentProcessingError: If the record does not name its parent.
    """

    parent_id = str(record.metadata.get("parent_source_item_id") or "")
    locator = parent_id.rpartition("/")[2].strip()
    if not locator:
        raise DocumentProcessingError(f"Attachment record {record.id!r} does not name its parent")
    return locator
