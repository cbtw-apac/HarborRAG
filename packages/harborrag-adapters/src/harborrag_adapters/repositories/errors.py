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


class MissingOptionalDependencyError(ImportError):
    """A provider needs an optional extra that is not installed.

    Providers used to signal this by raising ``ImportError`` with an exact
    wording that the registry matched with a regular expression. A provider
    that worded it differently, or an ``ImportError`` raised by a transitive
    import, fell through as a bare traceback instead of the configuration
    error naming the extra. The message is still formatted the same way so
    existing tests and log lines are unchanged.
    """

    def __init__(self, distribution: str) -> None:
        super().__init__(f"{distribution} is not installed")
        self.distribution = distribution


class SchemaAheadOfBuildError(RuntimeError):
    """The control database is stamped at a revision this build does not ship.

    A newer build migrated it, so there is no path to this build's head, and running this
    build's code against the newer schema is unsafe. The message names both revisions
    and the two ways out, instead of leaving them in an Alembic traceback.
    """

    def __init__(self, *, database_revision: str, build_head: str) -> None:
        super().__init__(
            f"control-plane database is at schema revision {database_revision}, newer than "
            f"this build's head {build_head}; it was migrated by a newer HarborRAG build. "
            f"Run that build, or downgrade the database to {build_head} with its migrations"
        )
        self.database_revision = database_revision
        self.build_head = build_head


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
