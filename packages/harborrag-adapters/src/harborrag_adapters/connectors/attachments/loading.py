"""Loader that turns an admitted attachment record into its own raw document.

Does NOT check the parent's access; connectors do that before loading.
"""

from __future__ import annotations

import hashlib

from harborrag_adapters.connectors.acl import SOURCE_ACL_METADATA_KEY
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .records import CONTAINER_IDENTITY_KEYS, attachment_descriptor_from_record
from .sources import AttachmentSourceGateway


class AttachmentDocumentLoader:
    """Loader of admitted attachment records as independent raw documents."""

    def __init__(self, gateway: AttachmentSourceGateway) -> None:
        self._gateway = gateway

    def load(self, record: SourceRecord) -> RawDocument:
        """Download an attachment and wrap it as a raw document.

        The document keeps the record's container identity and source ACL, and relates
        to its parent with ``attached_to``.

        Args:
            record: Attachment binding record built by ``attachment_source_record``.

        Returns:
            Raw document with a SHA-256 checksum, sourced at
            ``<parent url>#attachment-<id>`` when the parent URL is known.

        Raises:
            ValueError: If the record is not an attachment binding.
            FetchError: If the download is empty or exceeds the size limit.
        """

        descriptor = attachment_descriptor_from_record(record)
        content = self._gateway.fetch(descriptor)
        parent_id = str(record.metadata["parent_source_item_id"])
        parent_url = record.metadata.get("parent_source_url")
        source = f"{parent_url}#attachment-{descriptor.attachment_id}" if parent_url else record.id
        return RawDocument(
            id=record.id,
            source=source,
            content=content,
            content_type=descriptor.media_type,
            metadata={
                # The parent's source ACL travels with it: an attachment is readable by no
                # more people than its parent.
                **{
                    key: record.metadata[key]
                    for key in (*CONTAINER_IDENTITY_KEYS, SOURCE_ACL_METADATA_KEY)
                    if key in record.metadata
                },
                "attachment_id": descriptor.attachment_id,
                "title": descriptor.title,
                "filename": descriptor.title,
                "media_type": descriptor.media_type,
                "size_bytes": descriptor.size_bytes,
                "source_version": descriptor.source_version,
                "checksum": hashlib.sha256(content).hexdigest(),
                "parent_source_item_id": parent_id,
                "relations": [
                    {
                        "predicate": "attached_to",
                        "target_id": parent_id,
                        "target_type": "document",
                        "metadata": {"source_relation_version": (descriptor.source_version)},
                    }
                ],
            },
            raw={"attachment_id": descriptor.attachment_id},
        )
