"""Process-wide logging configuration for HarborRAG entry points.

Every HarborRAG module logs under the ``harborrag`` logger namespace, but no
library module installs a handler. Without one, ``logging`` falls back to its
last-resort handler at WARNING, so every ``logger.info`` and ``logger.debug``
call in the runtime, workers, and transports is silently discarded -- which is
how a Temporal worker container ends up emitting nothing at all.

Call :func:`configure_logging` from process entry points only (the worker
module, the CLI entry point, the API factory). Importing modules must never
call it: doing so would hijack logging configuration for applications that
embed HarborRAG as a library.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TextIO

ROOT_LOGGER_NAME = "harborrag"
LEVEL_ENV_VAR = "HARBORRAG_LOG_LEVEL"
DEFAULT_LEVEL = "INFO"

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


class _ManagedStreamHandler(logging.StreamHandler):
    """Marks the handler this module owns so repeat calls never stack handlers."""


def resolve_level(level: str | int | None = None) -> int:
    """Resolve an explicit level, else ``HARBORRAG_LOG_LEVEL``, else INFO.

    Unrecognised names fall back to INFO rather than raising: a typo in an
    operator's environment variable must not stop a worker from booting.
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

    Idempotent: a repeated call re-applies the level to the handler this
    module already installed instead of adding a second one, so a process that
    configures logging twice still logs each record once.

    ``propagate`` is disabled because hosts such as uvicorn configure the root
    logger themselves; propagating would emit every HarborRAG record twice.
    Records go to stderr so that machine-readable command output on stdout
    (``--json``) stays parseable when logging is turned up.
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
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    logger.addHandler(handler)
    return logger
