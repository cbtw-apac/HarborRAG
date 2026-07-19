class HarborError(Exception):
    """Base HarborRAG error."""


class HarborImportError(HarborError):
    """Raised when a required module is not installed."""


class HarborConfigError(HarborError):
    """Raised when there is a configuration error."""


class HarborConnectionError(HarborError):
    """Raised when there is a connection error to an external service."""


class HarborNotSupportedError(HarborError):
    """Raised when a feature or operation is not supported."""
