"""Public outcomes for exceptions raised while serving an HTTP tool call.

Tool and adapter exceptions can carry storage paths, upstream responses or
credentials, and a pydantic ``ValidationError`` embeds every submitted value.
Only text this transport authored, and the server's own policy refusals, is
returned verbatim; everything else collapses to its class name. This mirrors
the application's ``public_error_message``, which mcp-server cannot import.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

_MAX_REPORTED_FIELDS = 10
_MAX_FIELD_PATH_LENGTH = 80


class ToolRequestError(ValueError):
    """A request-shape problem the HTTP transport detected; its message is public."""


def _validation_message(exc: ValidationError) -> str:
    paths: list[str] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        path = ".".join(str(part) for part in error["loc"])[:_MAX_FIELD_PATH_LENGTH] or "<root>"
        if path not in paths:
            paths.append(path)
    shown = ", ".join(paths[:_MAX_REPORTED_FIELDS])
    hidden = len(paths) - _MAX_REPORTED_FIELDS
    suffix = f" (+{hidden} more)" if hidden > 0 else ""
    return f"Invalid input for field(s): {shown}{suffix}"


def public_tool_error(exc: BaseException) -> tuple[str, int]:
    """Return a reviewed public message and HTTP status for a failed tool call."""

    if isinstance(exc, ToolRequestError):
        return str(exc), 422
    if isinstance(exc, json.JSONDecodeError):
        return "request body must be valid JSON", 422
    if isinstance(exc, UnicodeDecodeError):
        return "request body must be UTF-8", 422
    if isinstance(exc, ValidationError):
        return _validation_message(exc), 422
    if isinstance(exc, PermissionError):
        # The server's own policy refusals carry no errno; an OS PermissionError
        # names a filesystem path.
        return (str(exc) if exc.errno is None else "permission denied"), 403
    if isinstance(exc, (TypeError, ValueError)):
        return f"invalid tool request ({type(exc).__name__})", 422
    return f"tool execution failed ({type(exc).__name__})", 500
