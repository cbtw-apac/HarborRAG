"""Failure summaries that are safe to persist in Temporal history."""

from __future__ import annotations

from temporalio.exceptions import (
    ApplicationError,
    CancelledError,
    TerminatedError,
    TimeoutError,
)

_MAX_CAUSE_DEPTH = 16


def durable_failure(error: BaseException) -> tuple[str, str]:
    """Return a stable type and non-sensitive operator message."""

    cause = getattr(error, "cause", None)
    failure = cause if isinstance(cause, BaseException) else error
    declared_type = getattr(failure, "type", None)
    failure_type = (
        declared_type
        if isinstance(declared_type, str) and declared_type.strip()
        else type(failure).__name__
    )
    return (
        failure_type,
        "operation failed; inspect restricted worker logs using workflow identifiers",
    )


def terminal_failure_code(error: BaseException) -> str:
    """Return a safe code for the root of a Temporal failure chain.

    Walks ``.cause`` through workflow, child-workflow and activity wrappers and
    returns the first ``ApplicationError.type``. Messages are never read: they
    may carry third-party text.
    """

    current: BaseException | None = error
    for _ in range(_MAX_CAUSE_DEPTH):
        if current is None:
            break
        if isinstance(current, ApplicationError):
            declared = current.type
            if isinstance(declared, str) and declared.strip():
                return declared
            return "unknown"
        if isinstance(current, TimeoutError):
            return "timeout"
        if isinstance(current, TerminatedError):
            return "terminated"
        if isinstance(current, CancelledError):
            return "cancelled"
        cause = getattr(current, "cause", None)
        current = cause if isinstance(cause, BaseException) else None
    return "unknown"


def durable_skip_reason(error: BaseException) -> str | None:
    """Return the skip reason an activity attached to its failure, if any."""

    cause = getattr(error, "cause", None)
    failure = cause if isinstance(cause, BaseException) else error
    details = getattr(failure, "details", ())
    if details and isinstance(details[0], str) and details[0].strip():
        return details[0]
    return None
