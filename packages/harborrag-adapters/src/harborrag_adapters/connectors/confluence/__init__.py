"""Confluence connector public API."""

from harborrag_adapters.connectors.attachments.classification import FileType
from harborrag_adapters.connectors.confluence.config import (
    ConfluenceDeploymentType,
    ConfluenceSpaceConfig,
)
from harborrag_adapters.connectors.confluence.connector import ConfluenceConnector
from harborrag_adapters.connectors.confluence.document_transform import (
    ConfluenceDocumentTransform,
)
from harborrag_adapters.connectors.confluence.schemas import (
    ConfluenceCommentMetadata,
    ConfluenceHierarchyMetadata,
    ConfluenceMetadata,
    ConfluencePageReference,
)

__all__ = [
    "ConfluenceCommentMetadata",
    "ConfluenceConnector",
    "ConfluenceDeploymentType",
    "ConfluenceDocumentTransform",
    "ConfluenceHierarchyMetadata",
    "ConfluenceMetadata",
    "ConfluencePageReference",
    "ConfluenceSpaceConfig",
    "FileType",
]
