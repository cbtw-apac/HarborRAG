"""Base and shared HarborRAG error types."""


class HarborError(Exception):
    """Base HarborRAG error."""


class HarborConnectionError(HarborError):
    """Raised when there is a connection error to an external service."""


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


class HarborNoIndexedContentError(HarborError):
    """Nothing has been ingested yet, so there is nothing to retrieve over.

    Distinct from ``HarborNotFoundError`` (the caller named something that
    does not exist) and from an unavailable backend: the service is healthy
    and the request is well-formed, the index simply has no content. That is
    a state the caller resolves by ingesting, so it must not be reported as a
    fault they should retry.
    """


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


class HarborAuthorizationUnavailableError(HarborUnavailableError):
    """The server could not verify the current reader authorization decision."""


class HarborValidationError(HarborError):
    """Request payload failed domain validation."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, object] = details or {}


class HarborConflictError(HarborError):
    """Operation conflicts with current resource state."""


class HarborPreconditionFailedError(HarborConflictError):
    """A conditional write found a different resource version than the caller expected."""

    error_code = "precondition_failed"


class HarborImmutableFieldError(HarborConflictError):
    """A write tried to change a field that is fixed once the resource exists."""

    error_code = "immutable_field"

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, object] = {"field": field} if field else {}


class HarborAlreadyRunningError(HarborConflictError):
    """A run, sync or job is already active for the resource; ``details`` points to it."""

    error_code = "already_running"

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, object] = details or {}


class HarborInUseError(HarborConflictError):
    """Other resources still depend on this one; ``details`` lists them."""

    error_code = "in_use"

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, object] = details or {}


class HarborAuthError(HarborError):
    """Authentication failed, or the principal lacks the required role.

    forbidden=False -> not authenticated (401); True -> authenticated
    but not allowed (403)
    """

    def __init__(self, message: str, forbidden: bool = False) -> None:
        super().__init__(message)
        self.forbidden = forbidden


class UnknownReferenceError(HarborValidationError):
    """A project or connection the request names does not exist in the tenant."""

    error_code = "reference_not_found"


class ConnectionOutOfScopeError(HarborValidationError):
    """A requested connection sits outside the session's or the question's projects."""

    error_code = "connection_out_of_scope"


class ChatModelUnavailableError(HarborUnavailableError):
    """No chat model answered: none is configured, or the provider is down."""

    error_code = "llm_unavailable"


class ModeUnavailableError(HarborValidationError):
    """The chat mode is not enabled on this install."""

    error_code = "mode_unavailable"


class CompletionInProgressError(HarborConflictError):
    """The first request with this idempotency key is still being answered."""

    error_code = "session_busy"


class StorageNotFoundError(RuntimeError):
    """A storage backend has no record of the requested resource.

    Base class of the adapter-level not-found error, so use cases catch it without
    importing an adapter.
    """


class IngestionNotFoundError(HarborNotFoundError):
    """The named ingestion does not exist for the caller's tenant."""

    error_code = "not_found"


class IngestionAlreadyCompletedError(HarborConflictError):
    """The ingestion already reached a terminal state."""

    error_code = "invalid_state"


class IngestionAlreadyPausedError(HarborConflictError):
    """The ingestion is already paused."""

    error_code = "invalid_state"


class IngestionAlreadyRunningError(HarborConflictError):
    """The ingestion is already running."""

    error_code = "already_running"


class IngestionActionStateError(HarborConflictError):
    """The requested action is not allowed in the ingestion's current state."""

    error_code = "invalid_state"


class IngestionIdempotencyConflictError(HarborConflictError):
    """An idempotency key was reused for a different ingestion request."""

    error_code = "invalid_state"


class IngestionRetryConflictError(HarborConflictError):
    """There is nothing to retry, or the run is not yet complete enough to retry."""

    error_code = "invalid_state"


class IngestionCursorError(HarborValidationError):
    """A pagination cursor is malformed or does not match the request filters."""

    error_code = "validation_error"


class IngestionFailureIdError(HarborValidationError):
    """A failure ID is malformed."""

    error_code = "validation_error"
