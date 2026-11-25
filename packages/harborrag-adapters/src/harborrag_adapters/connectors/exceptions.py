from urllib.parse import urlparse, urlunparse

from harborrag_core.ingestion import (
    SourceAuthenticationError,
    SourceAuthorizationError,
    SourceItemNotFoundError,
    SourceUnavailableError,
)

_RETRYABLE_CLIENT_STATUS = frozenset({408, 429})


def _redact_url(url: str) -> str:
    """Strip userinfo, query, and fragment before a URL reaches an error message.

    Kept local (not imported from ``connectors.policies.http``) because that
    module's package imports back from this one, which would create a
    circular import.
    """
    parsed = urlparse(url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


class ConnectorError(Exception):
    """Base exception for connector errors."""


class ConnectorNotFoundError(ConnectorError):
    """Raised when a connector provider is not registered."""


class ConnectorNotInitializedError(ConnectorError):
    """Raised when the connector is not properly initialized."""


class AuthenticationError(SourceAuthenticationError, ConnectorError):
    """Raised when source credentials are rejected as invalid for the whole connector."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        """Carry the originating HTTP status so callers/logs can surface it without parsing the message."""
        super().__init__(message)
        self.status_code = status_code


class AuthorizationError(SourceAuthorizationError, ConnectorError):
    """Raised when credentials are valid but lack permission/scope for a request."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        """Carry the originating HTTP status so callers/logs can surface it without parsing the message."""
        super().__init__(message)
        self.status_code = status_code


class FetchError(SourceUnavailableError, ConnectorError):
    """Raised when a connector cannot fetch source data.

    A 4xx answer other than 408 or 429 is not retryable: the source refuses the
    request itself, and asking again gets the same answer.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        """Carry the originating HTTP status/body so callers can inspect them without parsing the message."""
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.retryable = type(self).retryable and not _is_permanent_status(status_code)


def _is_permanent_status(status_code: int | None) -> bool:
    """Return whether an HTTP status is a client error that retrying cannot fix."""
    return (
        status_code is not None
        and 400 <= status_code < 500
        and status_code not in _RETRYABLE_CLIENT_STATUS
    )


class ItemNotFoundError(FetchError, SourceItemNotFoundError):
    """Raised when the source answers HTTP 404 for one item.

    Still a ``FetchError`` for callers that handle fetch failures generically, but
    classified as not found rather than transient: Jira answers 404 for an issue
    that was deleted -- or hidden from this account -- after discovery listed it,
    and retrying returns the same 404.
    """

    retryable = False


class RefusedResponseError(FetchError):
    """The source answered with something the connector will not read.

    Examples are a body over its size cap, a redirect to an untrusted origin and a
    URL outside the configured origin. Asking again gets the same answer, so it is
    never retried.
    """

    retryable = False


class RateLimitError(FetchError):
    """The source system rate-limits the connector.

    It stays retryable whatever its HTTP status, since GitHub answers a secondary
    rate limit with 403.

    Args:
        message: Safe description of the limit.
        status_code: HTTP status the source answered with.
        detail: Safe excerpt of the response body.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, detail=detail)
        self.retryable = type(self).retryable


class DocumentProcessingError(ConnectorError):
    """Raised when there is an error processing a document."""


class HTTPRequestError(ConnectorError):
    """Raised when there is an error with HTTP requests."""

    def __init__(self, url: str, status_code: int | None = None, message: str | None = None):
        """Capture request context while preserving a standard exception message."""
        self.url = url
        self.status_code = status_code
        self.message = message
        safe_url = _redact_url(url)
        super().__init__(f"HTTP request failed for {safe_url}: {message or 'Unknown error'}")
