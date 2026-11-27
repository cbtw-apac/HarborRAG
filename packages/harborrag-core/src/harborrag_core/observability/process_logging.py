"""Process-wide logging configuration for HarborRAG entry points.

Only process entry points (the worker module, the CLI, the API factory, the MCP server)
call :func:`configure_logging`. A library module that called it would take over logging
for any application that embeds HarborRAG.

Typical usage example:

    configure_logging()
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TextIO

ROOT_LOGGER_NAME = "harborrag"
LEVEL_ENV_VAR = "HARBORRAG_LOG_LEVEL"
DEFAULT_LEVEL = "INFO"
TRACE_ID_FIELD = "trace_id"
SPAN_ID_FIELD = "span_id"

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(module)s.%(funcName)s:%(lineno)d %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


class _ManagedStreamHandler(logging.StreamHandler[TextIO]):
    """The handler this module owns, so a repeated call never stacks handlers."""


class _TraceContextFormatter(logging.Formatter):
    """Format a record and append its trace and span IDs when a filter set them."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the formatted line, ending in ``trace_id=… span_id=…`` when traced."""
        line = super().format(record)
        trace_id = getattr(record, TRACE_ID_FIELD, None)
        if not isinstance(trace_id, str):
            return line
        span_id = getattr(record, SPAN_ID_FIELD, "")
        return f"{line} {TRACE_ID_FIELD}={trace_id} {SPAN_ID_FIELD}={span_id}"


def resolve_level(level: str | int | None = None) -> int:
    """Resolve an explicit level, else ``HARBORRAG_LOG_LEVEL``, else INFO.

    An unrecognised name falls back to INFO, so a typo in an operator's environment
    never stops a worker from starting.

    Args:
        level: A level name or number. The environment variable is read when omitted.

    Returns:
        The numeric logging level.
    """
    candidate = level if level is not None else os.getenv(LEVEL_ENV_VAR, DEFAULT_LEVEL)
    if isinstance(candidate, int):
        return candidate
    resolved = logging.getLevelNamesMapping().get(candidate.strip().upper())
    return resolved if resolved is not None else logging.INFO


def configure_logging(
    level: str | int | None = None,
    *,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Attach exactly one stream handler to the ``harborrag`` logger.

    A repeated call re-applies the level to the installed handler instead of adding
    a second one. ``propagate`` is turned off because hosts such as uvicorn configure
    the root logger themselves. Records go to stderr, so command output on stdout
    stays parseable. A record that carries ``trace_id`` and ``span_id`` prints them
    at the end of its line.

    Args:
        level: A level name or number. ``HARBORRAG_LOG_LEVEL`` is read when omitted.
        stream: Where records are written. Standard error when omitted.

    Returns:
        The configured ``harborrag`` logger.
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    resolved = resolve_level(level)
    logger.setLevel(resolved)
    logger.propagate = False
    for handler in logger.handlers:
        if isinstance(handler, _ManagedStreamHandler):
            handler.setLevel(resolved)
            return logger
    handler = _ManagedStreamHandler(stream if stream is not None else sys.stderr)
    handler.setLevel(resolved)
    handler.setFormatter(_TraceContextFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    logger.addHandler(handler)
    return logger
