"""Secret-safe application error envelopes."""

from __future__ import annotations

from logging import Logger

from harborrag_core.contracts.errors import (
    HarborConflictError,
    HarborNotFoundError,
    HarborValidationError,
)
from harborrag_runtime.errors import (
    RuntimeConfigurationError,
    RuntimeConnectionError,
    WorkerStartupError,
    WorkflowNotFoundError,
    WorkflowNotRunningError,
    WorkflowOperationError,
    WorkflowRunAlreadyStartedError,
    WorkflowSubmissionError,
)

from .schemas import AppResponse


class IngestionNotFoundError(HarborNotFoundError):
    error_code = "INGESTION_NOT_FOUND"


class IngestionAlreadyCompletedError(HarborConflictError):
    error_code = "INGESTION_ALREADY_COMPLETED"


class IngestionIdempotencyConflictError(HarborConflictError):
    error_code = "INGESTION_IDEMPOTENCY_CONFLICT"


class IngestionRetryConflictError(HarborConflictError):
    error_code = "INGESTION_NO_RETRYABLE_FAILURES"


class IngestionCursorError(HarborValidationError):
    error_code = "INGESTION_CURSOR_INVALID"


class IngestionStatusFilterError(HarborValidationError):
    error_code = "INGESTION_STATUS_INVALID"


# Error types whose messages are authored inside HarborRAG at the Temporal
# boundary and only ever interpolate caller-supplied identifiers (run IDs,
# connector names, queue names) -- never provider responses, credentials, or
# storage layout. Their messages are the operator's primary diagnostic, so
# they are forwarded verbatim.
#
# This is deliberately an explicit tuple rather than an isinstance check
# against a shared base class: a base-class check would silently admit every
# future subclass whose message nobody reviewed, which is the exact leak this
# module exists to prevent. Adding a type here means accepting its messages
# as public.
#
# ``ValueError`` is included to preserve the original behaviour for local
# argument validation raised by the application services themselves.
_PUBLIC_MESSAGE_TYPES: tuple[type[Exception], ...] = (
    RuntimeConfigurationError,
    RuntimeConnectionError,
    WorkerStartupError,
    WorkflowNotFoundError,
    WorkflowNotRunningError,
    WorkflowOperationError,
    WorkflowRunAlreadyStartedError,
    WorkflowSubmissionError,
    ValueError,
)


def public_error_message(exc: Exception) -> str:
    """Expose reviewed local messages, never arbitrary provider messages.

    Anything outside the allowlist collapses to its class name. That keeps
    third-party and adapter messages -- which may carry credentials, upstream
    response bodies, or internal storage paths -- out of CLI output and HTTP
    responses. Callers are expected to ``logger.exception`` the original so the
    full detail stays available to whoever operates the process.
    """

    if isinstance(exc, _PUBLIC_MESSAGE_TYPES):
        return str(exc) or type(exc).__name__
    return type(exc).__name__


def failure_response(
    logger: Logger,
    exc: Exception,
    message: str = "Application service call failed",
    *args: object,
) -> AppResponse:
    """Log private detail while returning only a reviewed public message."""

    public = public_error_message(exc)
    if public == type(exc).__name__:
        logger.error(message, *args, exc_info=exc)
    else:
        logger.debug(message, *args, exc_info=exc)
    return AppResponse(
        False,
        data={"error_type": type(exc).__name__},
        error=public,
    )
