"""Run-level source errors and how the runtime should react to them.

Only run-level problems raise. A problem with one item is yielded as an
:class:`~harborrag_core.sources.events.ItemFailure` instead. Every class here
extends the existing ingestion error tree, so the current failure classifier
already treats them correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_core.ingestion.errors import (
    IngestionDomainError,
    SourceAuthenticationError,
    SourceAuthorizationError,
    SourceForbiddenError,
    SourceItemNotFoundError,
    SourceUnavailableError,
)


class AuthError(SourceAuthenticationError):
    """Credentials were rejected after one refresh, or the grant was revoked."""


class Forbidden(SourceAuthorizationError):
    """Credentials are valid but lack access to what was asked for."""


class ItemNotFound(SourceItemNotFoundError):
    """The source does not serve the requested item."""


class Transient(SourceUnavailableError):
    """A server or network failure that outlasted the retry policy."""


class RateLimited(SourceUnavailableError):
    """The source asked for a wait longer than the client may sleep.

    The runtime reschedules the run at ``retry_at`` and resumes from the last
    checkpoint instead of blocking a worker.
    """

    def __init__(self, retry_at: datetime, message: str = "") -> None:
        if retry_at.tzinfo is None:
            raise ValueError("retry_at must be timezone-aware")
        self.retry_at = retry_at.astimezone(UTC)
        super().__init__(message or f"rate limited until {self.retry_at.isoformat()}")


class ConfigError(IngestionDomainError, HarborConfigurationError):
    """The configuration cannot work: bad query, unknown project, blocked host.

    Retrying does not help; an admin has to change the configuration.
    """

    retryable = False


class ErrorCategory(StrEnum):
    """What the runtime does about a run-level error."""

    NEEDS_REAUTH = "needs_reauth"
    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"
    CONFIG = "config"
    UNKNOWN = "unknown"


_CATEGORIES: tuple[tuple[type[BaseException], ErrorCategory], ...] = (
    (RateLimited, ErrorCategory.RATE_LIMITED),
    (SourceForbiddenError, ErrorCategory.NEEDS_REAUTH),
    (ConfigError, ErrorCategory.CONFIG),
    (HarborConfigurationError, ErrorCategory.CONFIG),
    (SourceUnavailableError, ErrorCategory.TRANSIENT),
    (ConnectionError, ErrorCategory.TRANSIENT),
    (TimeoutError, ErrorCategory.TRANSIENT),
)


def classify(error: BaseException) -> ErrorCategory:
    """Map a run-level error to the runtime action it calls for."""

    for error_type, category in _CATEGORIES:
        if isinstance(error, error_type):
            return category
    return ErrorCategory.UNKNOWN
