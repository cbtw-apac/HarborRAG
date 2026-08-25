from .loading import AttachmentDocumentLoader
from .processing import (
    AttachmentMetadata,
    AttachmentProcessor,
    CustomAttachmentParser,
    FileType,
)
from .records import (
    CONTAINER_IDENTITY_KEYS,
    attachment_descriptor_from_record,
    attachment_source_record,
    is_attachment_record,
)
from .selection import (
    attachment_ids_from_filters,
    select_attachment_payloads,
)
from .sources import (
    AttachmentSourceDescriptor,
    AttachmentSourceGateway,
    AttachmentSourcePolicy,
)

__all__ = [
    "CONTAINER_IDENTITY_KEYS",
    "AttachmentDocumentLoader",
    "AttachmentMetadata",
    "AttachmentProcessor",
    "AttachmentSourceDescriptor",
    "AttachmentSourceGateway",
    "AttachmentSourcePolicy",
    "CustomAttachmentParser",
    "FileType",
    "attachment_descriptor_from_record",
    "attachment_ids_from_filters",
    "attachment_source_record",
    "is_attachment_record",
    "select_attachment_payloads",
]
