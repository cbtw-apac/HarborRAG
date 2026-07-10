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

    def __init__(
        self, url: str, status_code: int | None = None, message: str | None = None
    ):
        """Capture request context while preserving a standard exception message."""
        self.url = url
        self.status_code = status_code
        self.message = message
        super().__init__(f"HTTP request failed for {url}: {message or 'Unknown error'}")
