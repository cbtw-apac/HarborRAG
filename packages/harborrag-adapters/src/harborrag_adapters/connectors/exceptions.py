"""Connector error hierarchy mapped onto the core ingestion source errors.

Authentication, authorization, fetch and not-found errors inherit from both the core
error and ``ConnectorError``.
"""

from urllib.parse import urlparse, urlunparse

from harborrag_core.ingestion import (
    SourceAuthenticationError,
    SourceAuthorizationError,
    SourceItemNotFoundError,
    SourceUnavailableError,
)


def _redact_url(url: str) -> str:
    """Strip userinfo, query and fragment from a URL for error messages."""
    parsed = urlparse(url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


class ConnectorError(Exception):
    """Base exception for connector errors."""


class ConnectorNotFoundError(ConnectorError):
    """Raised when a connector provider is not registered."""


# TODO: remove ``ConnectorNotInitializedError``; nothing outside tests raises it.
class ConnectorNotInitializedError(ConnectorError):
    """Raised when the connector is not properly initialized."""


class AuthenticationError(SourceAuthenticationError, ConnectorError):
    """Raised when source credentials are rejected as invalid for the whole connector."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        """Store the originating HTTP status.

        Args:
            message: Error message.
            status_code: HTTP status code of the failing response.
        """
        super().__init__(message)
        self.status_code = status_code


class AuthorizationError(SourceAuthorizationError, ConnectorError):
    """Raised when credentials are valid but lack permission/scope for a request."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        """Store the originating HTTP status.

        Args:
            message: Error message.
            status_code: HTTP status code of the failing response.
        """
        super().__init__(message)
        self.status_code = status_code


class FetchError(SourceUnavailableError, ConnectorError):
    """Raised when a connector cannot fetch source data."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        """Store the originating HTTP status and response detail.

        Args:
            message: Error message.
            status_code: HTTP status code of the failing response.
            detail: Response body or other detail text.
        """
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class ItemNotFoundError(FetchError, SourceItemNotFoundError):
    """Raised when the source answers HTTP 404 for one item.

    A ``FetchError`` classified as not found, not retryable: retrying returns the same 404.
    """

    retryable = False


class RateLimitError(FetchError):
    """Raised when the source system rate-limits the connector."""


class DocumentProcessingError(ConnectorError):
    """Raised when there is an error processing a document."""


class SourceRestrictedError(DocumentProcessingError):
    """Raised when describe or load reaches an item the restricted-content policy withholds.

    Covers explicit IDs and direct loads; search discovery reports a ``ConnectorSkip`` instead.
    """


# TODO: remove ``HTTPRequestError``; nothing outside tests raises it.
class HTTPRequestError(ConnectorError):
    """Raised when there is an error with HTTP requests."""

    def __init__(self, url: str, status_code: int | None = None, message: str | None = None):
        """Store the request context and build a message with the URL redacted.

        Args:
            url: Request URL; userinfo, query and fragment are dropped from the message.
            status_code: HTTP status code, if a response arrived.
            message: Failure description.
        """
        self.url = url
        self.status_code = status_code
        self.message = message
        safe_url = _redact_url(url)
        super().__init__(f"HTTP request failed for {safe_url}: {message or 'Unknown error'}")
