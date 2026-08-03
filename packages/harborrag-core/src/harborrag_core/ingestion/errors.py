from __future__ import annotations

from harborrag_core.contracts.errors import HarborError


class IngestionDomainError(HarborError):
    """Base error translated at adapter boundaries and classified by engine policy."""

    retryable: bool = False


class SourceUnavailableError(IngestionDomainError):
    retryable = True


class SourceForbiddenError(IngestionDomainError):
    pass


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
