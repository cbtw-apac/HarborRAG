from __future__ import annotations

from harborrag_core.contracts.errors import HarborConflictError, HarborError


class IngestionDomainError(HarborError):
    """Base error translated at adapter boundaries and classified by engine policy."""

    retryable: bool = False


class SourceUnavailableError(IngestionDomainError):
    retryable = True


class SourceItemNotFoundError(IngestionDomainError):
    """The source no longer serves this item: deleted, moved, or no longer visible.

    Unlike ``SourceUnavailableError`` this is not transient -- the same request gets
    the same answer -- so it is not retried. Scan-based removal reconciliation, not
    this error, decides whether an already-published version is retired.
    """


class SourceForbiddenError(IngestionDomainError):
    pass


class SourceAuthenticationError(SourceForbiddenError):
    """Credentials themselves are rejected as invalid (e.g. HTTP 401)."""


class SourceAuthorizationError(SourceForbiddenError):
    """Credentials are valid but lack permission/scope for the request (e.g. HTTP 403)."""


class ParserRejectedDocumentError(IngestionDomainError):
    pass


class UnsupportedDocumentError(ParserRejectedDocumentError):
    pass


class ChunkValidationError(IngestionDomainError):
    pass


class RepresentationProviderError(IngestionDomainError):
    retryable = True


class ProjectionWriteError(IngestionDomainError):
    retryable = True


class ProjectionVerificationError(IngestionDomainError):
    pass


class PublicationConflictError(IngestionDomainError):
    pass


class PublicationSupersededError(PublicationConflictError, HarborConflictError):
    """The candidate was built from an active version that is no longer current.

    Publication is compare-and-set on the document's active pointer: the caller names
    the version it built from, and a pointer that moved since -- a newer release, a
    reindex, a removal -- refuses the write. Callers treat it as "superseded, skip",
    never as a failure to retry: a newer version already stands.
    """


class ExecutionCapabilityError(IngestionDomainError):
    """The selected executor does not provide the requested durable operation."""
