"""Secret-safe application error envelopes."""

from __future__ import annotations

from harborrag_runtime.errors import (
    RuntimeConfigurationError,
    RuntimeConnectionError,
    WorkerStartupError,
    WorkflowNotFoundError,
    WorkflowNotRetryableError,
    WorkflowNotRunningError,
    WorkflowOperationError,
    WorkflowRunAlreadyStartedError,
    WorkflowSubmissionError,
)

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
    WorkflowNotRetryableError,
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
