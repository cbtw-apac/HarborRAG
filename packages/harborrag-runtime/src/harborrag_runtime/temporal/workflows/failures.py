"""Deterministic failure summaries safe for durable workflow history."""

from __future__ import annotations


def durable_failure(exc: BaseException) -> tuple[str, str]:
    """Return a stable type and non-sensitive operator message.

    Provider exception strings commonly contain URLs, query tokens, paths, or
    response excerpts. Workflow history stores this summary instead; detailed
    diagnostics remain in access-controlled worker logs.
    """

    cause = getattr(exc, "cause", None)
    failure = cause if isinstance(cause, BaseException) else exc
    return (
        type(failure).__name__,
        "operation failed; inspect restricted worker logs using workflow identifiers",
    )
