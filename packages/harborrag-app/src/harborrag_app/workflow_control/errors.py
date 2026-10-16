"""Secret-safe application error envelopes."""

from __future__ import annotations

from logging import Logger

from pydantic import ValidationError

from harborrag_core.contracts.errors import (
    HarborConflictError,
    HarborNotFoundError,
    HarborValidationError,
)
from harborrag_runtime.errors import (
    MissingOptionalDependencyError as MissingOptionalDependencyError,
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


class IngestionAlreadyPausedError(HarborConflictError):
    error_code = "INGESTION_ALREADY_PAUSED"


class IngestionAlreadyRunningError(HarborConflictError):
    error_code = "INGESTION_ALREADY_RUNNING"


class IngestionIdempotencyConflictError(HarborConflictError):
    error_code = "INGESTION_IDEMPOTENCY_CONFLICT"


class IngestionRetryConflictError(HarborConflictError):
    error_code = "INGESTION_NO_RETRYABLE_FAILURES"


class IngestionCursorError(HarborValidationError):
    error_code = "INGESTION_CURSOR_INVALID"


class IngestionStatusFilterError(HarborValidationError):
    error_code = "INGESTION_STATUS_INVALID"


class ScheduleNotFoundAppError(HarborNotFoundError):
    error_code = "SCHEDULE_NOT_FOUND"


class ScheduleAlreadyExistsError(HarborConflictError):
    error_code = "SCHEDULE_ALREADY_EXISTS"


class ScheduleManagedByConfigError(HarborConflictError):
    error_code = "SCHEDULE_MANAGED_BY_CONFIG"


class ScheduleInvalidError(HarborValidationError):
    error_code = "SCHEDULE_INVALID"


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
# argument validation raised by the application services themselves. Its
# pydantic subclass ``ValidationError`` is NOT forwarded verbatim: its text
# embeds every submitted value (``input_value=...``), so it is rendered as
# field paths only (``_validation_message``).


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


_MAX_REPORTED_FIELDS = 10
_MAX_FIELD_PATH_LENGTH = 80


def _validation_message(exc: ValidationError) -> str:
    """Name the invalid fields without echoing any submitted value.

    Only ``loc`` is used: ``msg`` can embed a value (custom validators often
    interpolate it), and ``input``/``ctx`` are values by definition.
    """

    paths: list[str] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        path = ".".join(str(part) for part in error["loc"])[:_MAX_FIELD_PATH_LENGTH] or "<root>"
        if path not in paths:
            paths.append(path)
    shown = ", ".join(paths[:_MAX_REPORTED_FIELDS])
    hidden = len(paths) - _MAX_REPORTED_FIELDS
    suffix = f" (+{hidden} more)" if hidden > 0 else ""
    return f"Invalid input for field(s): {shown}{suffix}"


def public_error_message(exc: Exception) -> str:
    """Expose reviewed local messages, never arbitrary provider messages.

    Anything outside the allowlist collapses to its class name. That keeps
    third-party and adapter messages -- which may carry credentials, upstream
    response bodies, or internal storage paths -- out of CLI output and HTTP
    responses. Callers are expected to ``logger.exception`` the original so the
    full detail stays available to whoever operates the process.
    """

    if isinstance(exc, ValidationError):
        return _validation_message(exc)
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
        # A reviewed public message is still a failed operation: WARNING keeps
        # it visible at production log levels. The record carries only the
        # sanitized public text (a pydantic error's own text embeds submitted
        # values); the full traceback stays at DEBUG.
        logger.warning(message + ": %s", *args, public)
        logger.debug(message, *args, exc_info=exc)
    return AppResponse(
        False,
        data={"error_type": type(exc).__name__},
        error=public,
    )
