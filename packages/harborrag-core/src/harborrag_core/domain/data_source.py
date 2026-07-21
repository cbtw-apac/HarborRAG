from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class DataSourceType(Enum):
    CONFLUENCE = "confluence"
    CONFLUENCE_CLOUD = "confluence_cloud"
    CONFLUENCE_DATACENTER = "confluence_datacenter"
    JIRA = "jira"
    JIRA_CLOUD = "jira_cloud"
    JIRA_DATACENTER = "jira_datacenter"
    GITHUB = "github"
    LOCAL_FILE = "local_file"
    LOCAL_DIRECTORY = "local_directory"


@dataclass(kw_only=True)
class DocumentMetadata:
    """
    Metadata about a document, including its source system, title, URL, author, timestamps, tags, and any additional fields.
    This class is used to provide structured information about a document, which can be useful for filtering, searching, and displaying documents in a user interface.
    """

    source_system: DataSourceType = field(
        metadata={
            "description": "The system where the document originated, e.g. 'confluence', 'jira', 'github', etc."
        }
    )
    record_id: str | None = field(
        default=None,
        metadata={
            "description": "The unique identifier of the document in the source system, e.g. page ID, issue key, file path, etc."
        },
    )
    title: str | None = field(
        default=None,
        metadata={"description": "Human-readable title of the document, if available"},
    )
    url: str | None = field(
        default=None,
        metadata={"description": "Canonical, clickable link back to the source"},
    )
    author: str | None = field(
        default=None, metadata={"description": "Author of the document"}
    )
    checksum: str | None = field(
        default=None,
        metadata={
            "description": "Checksum or hash of the document content for change detection"
        },
    )
    permissions: dict[str, Any] = field(
        default_factory=dict,
        metadata={
            "description": "Permissions or access control information for the document"
        },
    )
    created_at: datetime | None = field(
        default=None,
        metadata={"description": "Timestamp when the document was created"},
    )
    updated_at: datetime | None = field(
        default=None,
        metadata={"description": "Timestamp when the document was last updated"},
    )
    tags: list[str] = field(
        default_factory=list,
        metadata={
            "description": "Labels/topics, wherever the source has an equivalent concept"
        },
    )
    extra: dict[str, Any] = field(
        default_factory=dict,
        metadata={
            "description": "Additional metadata fields that don't fit into the predefined categories"
        },
    )
