from __future__ import annotations

import hashlib

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .records import attachment_descriptor_from_record
from .sources import AttachmentSourceGateway


class AttachmentDocumentLoader:
    """Fetch one admitted attachment as an independent raw document."""

    def __init__(self, gateway: AttachmentSourceGateway) -> None:
        self._gateway = gateway

    def load(self, record: SourceRecord) -> RawDocument:
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
