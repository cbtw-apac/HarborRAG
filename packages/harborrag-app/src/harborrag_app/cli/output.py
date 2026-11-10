"""Print an operation's body or error the way its HTTP route answers.

Handles:
- ``-o json``: the route's response body, serialized with its own response model and flags;
  ``-o yaml``: the same object as YAML; ``-o table``: the view ``views.render`` draws
- Errors on stderr: the HTTP error envelope in JSON or YAML mode, one line in table mode

Does NOT catch exceptions or choose exit codes; ``invoke`` does.

Start from: print_body, print_error.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from enum import StrEnum

import yaml

from harborrag_app.cli.views import render
from harborrag_app.operations.contract import dump_body
from harborrag_app.operations.errors import ErrorBody

DONE = "Done."
_TRACE_SHOWN = 8


class OutputFormat(StrEnum):
    """How a command prints its body and its errors."""

    TABLE = "table"
    JSON = "json"
    YAML = "yaml"


def print_body(operation_id: str, body: object, *, output: OutputFormat) -> None:
    """Print a successful operation's body to stdout.

    Args:
        operation_id: Key of ``OPERATIONS`` whose response model serializes ``body``.
        body: What the operation returned, unwrapped; ``None`` when it answers no body,
            which prints nothing, or ``Done.`` in table mode.
        output: Format to print.
    """
    if body is None:
        if output is OutputFormat.TABLE:
            print(DONE)
        return
    data = dump_body(operation_id, body)
    if output is OutputFormat.TABLE:
        render(operation_id, data)
    else:
        print(_document(data, output))


def print_error(error: ErrorBody, *, output: OutputFormat, trace_id: str) -> None:
    """Print a failure to stderr.

    Table mode prints ``error <status> <code>: <message>[; <field>: <issue>…] (trace <id>…)``;
    JSON and YAML print ``{"error": {"code", "message", "details", "trace_id"}}``.

    Args:
        error: The failure as ``error_body`` mapped it.
        output: Format the command prints in.
        trace_id: The invocation's trace id, in full; table mode shows its first 8 characters.
    """
    if output is OutputFormat.TABLE:
        print(_error_line(error, trace_id), file=sys.stderr)
        return
    envelope = {
        "error": {
            "code": error.code,
            "message": error.message,
            "details": error.details or None,
            "trace_id": trace_id,
        }
    }
    print(_document(envelope, output), file=sys.stderr)


def _document(data: object, output: OutputFormat) -> str:
    """``data`` as YAML for ``-o yaml``, else as indented JSON."""
    if output is OutputFormat.YAML:
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).rstrip("\n")
    return json.dumps(data, indent=2, ensure_ascii=False)


def _error_line(error: ErrorBody, trace_id: str) -> str:
    """The one-line table-mode error."""
    issues = "".join(f"; {field}: {issue}" for field, issue in _field_issues(error.details))
    return (
        f"error {error.status} {error.code}: {error.message}{issues} "
        f"(trace {trace_id[:_TRACE_SHOWN]}…)"
    )


def _field_issues(details: object) -> list[tuple[str, str]]:
    """``(field, issue)`` of each ``[{field, issue}]`` detail that names a field."""
    if not isinstance(details, list):
        return []
    return [
        (str(item["field"]), str(item.get("issue", "")))
        for item in details
        if isinstance(item, Mapping) and item.get("field")
    ]
