"""Failure summaries that are safe to persist in Temporal history."""

from __future__ import annotations


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


def durable_skip_reason(error: BaseException) -> str | None:
    """Return the skip reason an activity attached to its failure, if any."""

    cause = getattr(error, "cause", None)
    failure = cause if isinstance(cause, BaseException) else error
    details = getattr(failure, "details", ())
    if details and isinstance(details[0], str) and details[0].strip():
        return details[0]
    return None
