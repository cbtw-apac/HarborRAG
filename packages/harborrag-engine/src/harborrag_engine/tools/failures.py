"""The failure payload every retrieval tool returns, with a code that says whose problem it is.

A tool that catches an error returns ``{"ok": false, "error": <message>, "error_code": <code>}``
and logs the cause once. The message is the error's own text only for HarborRAG errors written
for callers (a bad argument, a feature not turned on, an empty index) and for the tool's own
argument parsing. Anything else, a pydantic ``ValidationError`` from a stored artifact
included, shows a generic message, so stored content and provider internals never reach a
caller.

Typical usage example:

    except Exception as error:
        return tool_failure("get_document_context", error, logger, "document context retrieval failed")
"""

from __future__ import annotations

import logging
from enum import StrEnum

from harborrag_core.contracts.errors import (
    HarborCapabilityError,
    HarborDeadlineExceeded,
    HarborNoIndexedContentError,
    HarborValidationError,
)
from harborrag_core.observability.log_events import FailureKind, log_failure

TOOL_FAILED_EVENT = "tool.call_failed"


class ToolErrorCode(StrEnum):
    """Why a tool call failed, so callers, traces and alerts can tell causes apart.

    Attributes:
        INVALID_REQUEST: The caller's arguments are wrong; fix and resend.
        QUERY_TOO_BROAD: The query outlived the store's deadline; narrow it.
        NOT_ENABLED: The deployment has not turned this feature on.
        NO_INDEXED_CONTENT: Nothing has been ingested for the tenant yet.
        NOT_CONFIGURED: The server has no backend for the tool; an operator fixes it.
        BACKEND_UNAVAILABLE: A store or model failed; the server's fault.
    """

    INVALID_REQUEST = "invalid_request"
    QUERY_TOO_BROAD = "query_too_broad"
    NOT_ENABLED = "not_enabled"
    NO_INDEXED_CONTENT = "no_indexed_content"
    NOT_CONFIGURED = "not_configured"
    BACKEND_UNAVAILABLE = "backend_unavailable"


SERVER_SIDE_ERROR_CODES = frozenset(
    {ToolErrorCode.NOT_CONFIGURED, ToolErrorCode.BACKEND_UNAVAILABLE}
)
"""Codes that are the server's fault and count against MCP availability."""

_CALLER_FACING: tuple[tuple[type[Exception], ToolErrorCode, FailureKind], ...] = (
    (HarborNoIndexedContentError, ToolErrorCode.NO_INDEXED_CONTENT, FailureKind.EXPECTED),
    (HarborCapabilityError, ToolErrorCode.NOT_ENABLED, FailureKind.EXPECTED),
    (HarborValidationError, ToolErrorCode.INVALID_REQUEST, FailureKind.CALLER),
)


def failure(message: str, code: ToolErrorCode) -> dict[str, object]:
    """Build the failure payload a tool returns.

    Args:
        message: What the caller is told; never store names or document text.
        code: Why the call failed.

    Returns:
        ``{"ok": False, "error": message, "error_code": code}``.
    """
    return {"ok": False, "error": message, "error_code": code.value}


def invalid_arguments(tool: str, error: Exception, logger: logging.Logger) -> dict[str, object]:
    """Log and return the failure for arguments the tool itself could not parse.

    Only for errors raised while reading the caller's arguments: their text describes the
    arguments, so the caller sees it.

    Args:
        tool: The tool's registered name.
        error: The error the argument parsing raised.
        logger: The tool module's logger.

    Returns:
        The ``invalid_request`` failure payload with the error's text.
    """
    code = ToolErrorCode.INVALID_REQUEST
    log_failure(
        logger, TOOL_FAILED_EVENT, error, FailureKind.CALLER, 2, tool=tool, error_code=code.value
    )
    return failure(str(error), code)


def tool_failure(
    tool: str,
    error: Exception,
    logger: logging.Logger,
    message: str | None = None,
) -> dict[str, object]:
    """Log a caught error once with its cause and return the tool's failure payload.

    Args:
        tool: The tool's registered name.
        error: The error the tool caught.
        logger: The tool module's logger.
        message: What the caller is told when the error's own text must not be shown:
            a deadline's narrowing hint, or the generic message for a backend failure.
            Defaults to ``<tool> failed``.

    Returns:
        The failure payload, with the error's code.
    """
    for error_type, code, kind in _CALLER_FACING:
        if isinstance(error, error_type):
            log_failure(logger, TOOL_FAILED_EVENT, error, kind, 2, tool=tool, error_code=code.value)
            return failure(str(error), code)
    if isinstance(error, HarborDeadlineExceeded):
        code, kind = ToolErrorCode.QUERY_TOO_BROAD, FailureKind.CALLER
    else:
        code, kind = ToolErrorCode.BACKEND_UNAVAILABLE, FailureKind.UNEXPECTED
    log_failure(logger, TOOL_FAILED_EVENT, error, kind, 2, tool=tool, error_code=code.value)
    return failure(message or f"{tool} failed", code)
