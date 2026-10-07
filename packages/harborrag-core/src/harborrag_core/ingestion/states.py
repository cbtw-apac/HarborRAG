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
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DocumentIngestionOutcome(StrEnum):
    """Bounded result returned by one document ingestion execution."""

    PUBLISHED = "published"
    UNCHANGED = "unchanged"
    FAILED = "failed"
    # A failure caused by the source item being gone (HTTP 404). It counts as a
    # failed document, but says nothing about shared dependencies, so it must not
    # trip the batch circuit breaker.
    SOURCE_ITEM_MISSING = "source_item_missing"

    @property
    def is_failure(self) -> bool:
        return self in (
            DocumentIngestionOutcome.FAILED,
            DocumentIngestionOutcome.SOURCE_ITEM_MISSING,
        )


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


# Safe error codes whose cause is the source item itself: re-running the document
# cannot change the outcome, so not even an explicit retry is offered.
_SOURCE_INHERENT_FAILURE_CODES = frozenset({"document_unsupported", "parser_rejected_document"})
# Deterministic failures a blanket "retry all failures" should skip: the same
# input fails the same way until code changes. An operator who has deployed a fix
# can still retry such a document by naming it.
_DETERMINISTIC_FAILURE_CODES = frozenset(
    {"chunk_invalid", "projection_verification_failed", "source_item_not_found"}
)


def _failure_code(safe_error_code: str) -> str:
    return safe_error_code.strip().lower().removeprefix("document_release_")


def is_retryable_failure_code(safe_error_code: str) -> bool:
    """Whether a blanket retry of failed documents should include this failure.

    This is about the retry-failures action, not Temporal's in-run retry policy.
    Infrastructure faults and replay conflicts qualify in every stage; before,
    any PersistCanonical or ChunkAndValidate failure was excluded by stage name.
    """

    code = _failure_code(safe_error_code)
    return (
        bool(code)
        and code not in _SOURCE_INHERENT_FAILURE_CODES
        and code not in _DETERMINISTIC_FAILURE_CODES
    )


def is_explicitly_retryable_failure_code(safe_error_code: str) -> bool:
    """Whether a document an operator names for retry may be retried."""

    code = _failure_code(safe_error_code)
    return bool(code) and code not in _SOURCE_INHERENT_FAILURE_CODES


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
