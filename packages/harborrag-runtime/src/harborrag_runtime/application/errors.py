"""Secret-safe application error envelopes.

Messages reach CLI output and HTTP responses, so only reviewed text is forwarded. The
allow-list of exception types is an explicit tuple, not an ``isinstance`` check against a
shared base class: a base-class check would silently admit every future subclass whose
message nobody reviewed. Adding a type to it means accepting its messages as public. Its
types are authored inside HarborRAG and interpolate only caller-supplied identifiers (run
IDs, connector names, queue names), never provider responses, credentials or storage
layout. ``ValueError`` stays on it for local argument validation raised by the application
services themselves.
"""

from __future__ import annotations

from logging import Logger

from pydantic import ValidationError

from harborrag_core.contracts.errors import (
    HarborValidationError,
    IngestionActionStateError,
    IngestionNotFoundError,
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


class SettingsLevelNotFoundError(HarborValidationError):
    """The settings level the caller named does not exist."""

    error_code = "reference_not_found"


_PUBLIC_MESSAGE_TYPES: tuple[type[Exception], ...] = (
    IngestionActionStateError,
    IngestionNotFoundError,
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


def validation_error_detail(exc: ValidationError) -> str:
    """Summarise a ValidationError as ``loc: msg`` pairs, without input values.

    A ValidationError is a ValueError, but its text embeds the offending input values (DSNs,
    keys), so only where and why each field failed is reported.
    """
    errors = exc.errors(include_input=False, include_url=False)
    if not errors:
        return "invalid settings"
    details = []
    for error in errors:
        loc = ".".join(str(part) for part in error["loc"])
        details.append(f"{loc}: {error['msg']}" if loc else str(error["msg"]))
    return "; ".join(details)


def public_error_message(exc: Exception) -> str:
    """Expose reviewed local messages, never arbitrary provider messages.

    Anything outside the allowlist collapses to its class name. That keeps
    third-party and adapter messages -- which may carry credentials, upstream
    response bodies, or internal storage paths -- out of CLI output and HTTP
    responses. Callers are expected to ``logger.exception`` the original so the
    full detail stays available to whoever operates the process.
    """
    if isinstance(exc, ValidationError):
        return validation_error_detail(exc)
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
