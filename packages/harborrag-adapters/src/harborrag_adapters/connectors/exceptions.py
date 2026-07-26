from urllib.parse import urlparse, urlunparse


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


class AuthenticationError(ConnectorError):
    """Raised when source credentials are rejected for the whole connector."""


class FetchError(ConnectorError):
    """Raised when a connector cannot fetch source data."""


class RateLimitError(FetchError):
    """Raised when the source system rate-limits the connector."""


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
