from __future__ import annotations

from harborrag_core.contracts.errors import HarborError


class IngestionDomainError(HarborError):
    """Base error translated at adapter boundaries and classified by engine policy."""

    retryable: bool = False


class SourceUnavailableError(IngestionDomainError):
    retryable = True


class SourceRetriesExhaustedError(SourceUnavailableError):
    """The source still failed transiently after the connector's own retries.

    The connector already spent its configured retry budget on this item, so
    the run moves on rather than multiplying that budget. The failure stays
    transient: a later run or a blanket retry of failed documents tries again.
    """

    retryable = False


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


class ExecutionCapabilityError(IngestionDomainError):
    """The selected executor does not provide the requested durable operation."""
