"""Compatibility exports for attachment schemas used by Confluence callers."""

from harborrag_adapters.connectors.attachments import (
    AttachmentMetadata,
    AttachmentStatus,
    CustomAttachmentParser,
    FileType,
)

__all__ = [
    "AttachmentMetadata",
    "AttachmentStatus",
    "CustomAttachmentParser",
    "FileType",
]
