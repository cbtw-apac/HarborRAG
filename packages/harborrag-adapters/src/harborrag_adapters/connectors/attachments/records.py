from __future__ import annotations

from collections.abc import Mapping

from harborrag_core.domain.source import SourceRecord

from .sources import AttachmentSourceDescriptor

_ATTACHMENT_BINDING = "ATTACHMENT"

# The container-identity keys an attachment inherits from its parent, named once so the
# record that carries them and the loader that forwards them cannot drift. Putting them on
# the SourceRecord is not enough on its own: AttachmentDocumentLoader rebuilds the raw
# document's metadata from the descriptor, so a key it does not forward never reaches
# provenance.extra and the graph projector never sees the container.
CONTAINER_IDENTITY_KEYS = ("space_id", "space_key", "project_id", "project_key")


def attachment_source_record(
    parent: SourceRecord,
    descriptor: AttachmentSourceDescriptor,
    *,
    inherited: Mapping[str, object] | None = None,
) -> SourceRecord:
    """Create a stable, independently dispatchable attachment source record.

    ``inherited`` carries the parent's *container* identity -- a Confluence space, a Jira
    project. An attachment is dispatched as its own source item and knows nothing about
    where its parent lives, so without this the graph projector finds no container and
    files it under the data source instead: measured on a live graph, 147 of 147
    attachments hung off the DataSource while every page hung off the space. The keys are
    the provider's own identity fields, so the attachment resolves to the *same* container
    node its parent does rather than minting a second one.
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
