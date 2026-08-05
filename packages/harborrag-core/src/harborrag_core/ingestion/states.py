from __future__ import annotations

from enum import StrEnum
from re import fullmatch
from typing import Self


class BindingKind(StrEnum):
    """Describe how an independently ingestible source object is bound."""

    ROOT = "ROOT"
    ATTACHMENT = "ATTACHMENT"
    EMBEDDED = "EMBEDDED"
    CONTAINED = "CONTAINED"


class SourceAdmissionDecision(StrEnum):
    """Describe the result of source admission and change detection."""

    NEW = "NEW"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"
    METADATA_CHANGED = "METADATA_CHANGED"
    FORCE_REPROCESS = "FORCE_REPROCESS"
    METADATA_ONLY = "METADATA_ONLY"
    UNSUPPORTED = "UNSUPPORTED"
    SECURITY_REJECTED = "SECURITY_REJECTED"
    REMOVED_CANDIDATE = "REMOVED_CANDIDATE"


class SourceScanState(StrEnum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class IngestionTaskState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DocumentVersionState(StrEnum):
    PENDING = "PENDING"
    RAW_CAPTURED = "RAW_CAPTURED"
    CANONICAL_READY = "CANONICAL_READY"
    CHUNKS_READY = "CHUNKS_READY"
    REPRESENTATIONS_READY = "REPRESENTATIONS_READY"
    PROJECTIONS_STAGED = "PROJECTIONS_STAGED"
    VERIFIED = "VERIFIED"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    FAILED = "FAILED"


class FailureCategory(StrEnum):
    TRANSIENT = "TRANSIENT"
    RATE_LIMITED = "RATE_LIMITED"
    PARSER_FALLBACKABLE = "PARSER_FALLBACKABLE"
    UNSUPPORTED = "UNSUPPORTED"
    SOURCE_FORBIDDEN = "SOURCE_FORBIDDEN"
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    CANONICAL_VALIDATION = "CANONICAL_VALIDATION"
    CHUNK_VALIDATION = "CHUNK_VALIDATION"
    ENCODER_FAILURE = "ENCODER_FAILURE"
    VECTOR_WRITE_FAILURE = "VECTOR_WRITE_FAILURE"
    GRAPH_WRITE_FAILURE = "GRAPH_WRITE_FAILURE"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    PUBLICATION_FAILURE = "PUBLICATION_FAILURE"


class KnowledgeNodeKind(StrEnum):
    """Broad storage labels; provider-specific shape belongs in ``entity_type``."""

    TENANT = "Tenant"
    DATA_SOURCE = "DataSource"
    SOURCE_ENTITY = "SourceEntity"
    DOCUMENT_VERSION = "DocumentVersion"
    STRUCTURE = "Structure"
    CHUNK = "Chunk"


class GraphOwnershipScope(StrEnum):
    """Lifecycle owner for a graph node or relationship."""

    TENANT = "TENANT"
    SOURCE_SCOPE = "SOURCE_SCOPE"
    DOCUMENT_VERSION = "DOCUMENT_VERSION"


class GraphEntityType(StrEnum):
    """Extensible semantic type independent from the broad graph label."""

    TENANT = "tenant"
    DATA_SOURCE = "data_source"
    GENERIC_SOURCE_ITEM = "generic_source_item"
    DOCUMENT_VERSION = "document_version"
    SECTION = "section"
    TABLE = "table"
    COMMENT = "comment"
    CHUNK = "chunk"

    CONFLUENCE_SPACE = "confluence_space"
    CONFLUENCE_PAGE = "confluence_page"
    CONFLUENCE_ATTACHMENT = "confluence_attachment"
    JIRA_PROJECT = "jira_project"
    JIRA_ISSUE = "jira_issue"
    JIRA_ATTACHMENT = "jira_attachment"
    GITHUB_OWNER = "github_owner"
    GITHUB_REPOSITORY = "github_repository"
    GITHUB_DIRECTORY = "github_directory"
    GITHUB_FILE = "github_file"
    GITHUB_REF = "github_ref"
    GITHUB_COMMIT = "github_commit"
    SHAREPOINT_SITE = "sharepoint_site"
    SHAREPOINT_DRIVE = "sharepoint_drive"
    SHAREPOINT_FOLDER = "sharepoint_folder"
    SHAREPOINT_FILE = "sharepoint_file"
    LOCAL_ROOT = "local_root"
    LOCAL_DIRECTORY = "local_directory"
    LOCAL_FILE = "local_file"

    @classmethod
    def _missing_(cls, value: object) -> Self | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().casefold()
        if fullmatch(r"[a-z][a-z0-9_-]{0,63}", normalized) is None:
            return None
        member = str.__new__(cls, normalized)
        member._name_ = f"CUSTOM_{normalized.upper().replace('-', '_')}"
        member._value_ = normalized
        return member


class CleanupJobState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ReindexJobState(StrEnum):
    """Describe the durable lifecycle of a connector-free reindex job."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
