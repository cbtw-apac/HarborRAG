class HarborError(Exception):
    """Base HarborRAG error."""


class HarborImportError(HarborError):
    """Raised when a required module is not installed."""


class HarborConnectionError(HarborError):
    """Raised when there is a connection error to an external service."""


class HarborNotSupportedError(HarborError):
    """Raised when a feature or operation is not supported."""


class HarborConfigurationError(HarborError):
    """Configuration is invalid."""


class HarborCapabilityError(HarborError):
    """Requested capability is not supported."""


class HarborSecurityError(HarborError):
    """Security policy rejected an operation."""


class HarborDeadlineExceeded(HarborError):
    """Operation exceeded its deadline."""


class HarborRateLimitError(HarborError):
    """Caller exceeded an API or provider-owned capacity limit."""

    def __init__(self, message: str, *, retry_after_seconds: int = 60) -> None:
        super().__init__(message)
        self.details: dict[str, object] = {"retry_after_seconds": retry_after_seconds}


class HarborNotFoundError(HarborError):
    """Request resource does not exist."""


class HarborSecretDecryptionError(HarborError):
    """A stored secret exists but cannot be decrypted with the configured key.

    Distinct from HarborNotFoundError: the ref is real and the row is present,
    but decryption failed -- almost always a key-rotation mistake (the secret
    was encrypted under a key that is no longer configured), not a missing
    resource. Conflating the two would mask that operational failure as an
    ordinary 404.
    """


class HarborUnavailableError(HarborError):
    """A required backing service is not configured or reachable."""


class HarborValidationError(HarborError):
    """Request payload failed domain validation."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, object] = details or {}


class HarborConflictError(HarborError):
    """Operation conflicts with current resource state."""


class HarborAuthError(HarborError):
    """Authentication failed, or the principal lacks the required role.

    forbidden=False -> not authenticated (401); True -> authenticated
    but not allowed (403)"""

    def __init__(self, message: str, forbidden: bool = False) -> None:
        super().__init__(message)
        self.forbidden = forbidden
