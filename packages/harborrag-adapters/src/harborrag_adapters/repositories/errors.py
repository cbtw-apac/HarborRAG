from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harborrag_core.storage import StorageFamily


@dataclass(slots=True)
class StorageErrorContext:
    """Carries sanitized diagnostic context for a storage failure."""

    family: StorageFamily
    backend: str
    instance_name: str
    operation: str
    retryable: bool = False
    status_code: str | int | None = None
    provider_request_id: str | None = None
    tenant_id: str | None = None
    resource_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class HarborStorageError(RuntimeError):
    """Stable base exception. Provider details remain available through chaining."""

    def __init__(
        self,
        message: str,
        *,
        context: StorageErrorContext,
        original: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.context = context
        self.original = original


class HarborStorageConfigurationError(HarborStorageError):
    """Raised when a storage operation fails because of configuration."""

    pass


class HarborStorageConnectionError(HarborStorageError):
    """Raised when a storage operation fails because of connection."""

    pass


class HarborStorageAuthenticationError(HarborStorageError):
    """Raised when a storage operation fails because of authentication."""

    pass


class HarborStorageAuthorizationError(HarborStorageError):
    """Raised when a storage operation fails because of authorization."""

    pass


class HarborStorageTimeoutError(HarborStorageError):
    """Raised when a storage operation fails because of timeout."""

    pass


class HarborStorageRateLimitError(HarborStorageError):
    """Raised when a storage operation fails because of rate limit."""

    pass


class HarborStorageConflictError(HarborStorageError):
    """Raised when a storage operation fails because of conflict."""

    pass


class HarborStorageNotFoundError(HarborStorageError):
    """Raised when a storage operation fails because of not found."""

    pass


class HarborStorageAlreadyExistsError(HarborStorageError):
    """Raised when a storage operation fails because of already exists."""

    pass


class HarborStorageValidationError(HarborStorageError):
    """Raised when a storage operation fails because of validation."""

    pass


class HarborStorageTransactionError(HarborStorageError):
    """Raised when a storage operation fails because of transaction."""

    pass


class HarborStorageMigrationError(HarborStorageError):
    """Raised when a storage operation fails because of migration."""

    pass


class HarborStorageCapabilityError(HarborStorageError):
    """Raised when a storage operation fails because of capability."""

    pass


class HarborStorageSerializationError(HarborStorageError):
    """Raised when a storage operation fails because of serialization."""

    pass


class HarborStorageIntegrityError(HarborStorageError):
    """Raised when a storage operation fails because of integrity."""

    pass


class HarborStorageLeaseError(HarborStorageError):
    """Raised when a storage operation fails because of lease."""

    pass


class HarborStorageCheckpointConflictError(HarborStorageConflictError):
    """Raised when a storage operation fails because of checkpoint conflict."""

    pass


class HarborObjectTooLargeError(HarborStorageValidationError):
    """Raised when HarborRAG detects object too large."""

    pass


class HarborVectorDimensionError(HarborStorageValidationError):
    """Raised when HarborRAG detects vector dimension."""

    pass


class HarborUnsafeQueryError(HarborStorageValidationError):
    """Raised when HarborRAG detects unsafe query."""

    pass
