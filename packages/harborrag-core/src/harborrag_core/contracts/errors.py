class HarborError(Exception):
    """Base HarborRAG error."""


class HarborConfigurationError(HarborError):
    """Configuration is invalid."""


class HarborCapabilityError(HarborError):
    """Requested capability is not supported."""


class HarborSecurityError(HarborError):
    """Security policy rejected an operation."""


class HarborDeadlineExceeded(HarborError):
    """Operation exceeded its deadline."""


class HarborNotFoundError(HarborError):
    """Request resource does not exist."""


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
