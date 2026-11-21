from __future__ import annotations

import hashlib
import logging
from typing import Any

from harborrag_adapters.parsers.common.resources import parse_input_suffix
from harborrag_core.domain.parser import ParseInput

PARSER_LOGGER_NAME = "harborrag.adapters.parsers"
_NAME_DIGEST_CHARS = 12


def _install_null_handler(logger: logging.Logger) -> None:
    """Avoid noisy parser logs unless the application configures logging."""
    if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
        logger.addHandler(logging.NullHandler())


_install_null_handler(logging.getLogger(PARSER_LOGGER_NAME))


def get_parser_logger(component: str | None = None) -> logging.Logger:
    """Return the parser subsystem logger or a named child logger."""
    if not component:
        return logging.getLogger(PARSER_LOGGER_NAME)

    normalized = component.strip().strip(".").lower().replace("-", "_")
    normalized = normalized.replace(" ", "_")
    if not normalized:
        return logging.getLogger(PARSER_LOGGER_NAME)
    return logging.getLogger(f"{PARSER_LOGGER_NAME}.{normalized}")


def parser_log_extra(
    *,
    input: ParseInput | None = None,
    parser_name: str | None = None,
    parser_engine: str | None = None,
    route_kind: str | None = None,
    route_key: str | None = None,
    **values: Any,
) -> dict[str, Any]:
    """Build safe structured logging fields for parser log records.

    These fields intentionally describe provenance and sizes, not document
    content. That keeps parser logs useful in production without putting source
    text into log sinks.
    """
    extra: dict[str, Any] = {}
    if parser_name:
        extra["harbor_parser"] = parser_name
    if parser_engine:
        extra["harbor_parser_engine"] = parser_engine
    if route_kind:
        extra["harbor_route_kind"] = route_kind
    if route_key:
        extra["harbor_route_key"] = route_key

    if input is not None:
        extra["harbor_input"] = input_label(input)
        extra["harbor_suffix"] = parse_input_suffix(input) or None
        extra["harbor_content_type"] = input.content_type

    for key, value in values.items():
        if value is not None:
            extra[f"harbor_{key}"] = value
    return extra


def input_label(input: ParseInput) -> str:
    """Return a log label for parser input: a digest of its name, its suffix and its size.

    A file name can name a person, so neither the name nor the path appears. The size
    counts bytes, or characters for text content, and is ``unknown`` for a path input.
    """
    name = input.filename or (str(input.path) if input.path is not None else "")
    digest = name_digest(name) if name else "memory"
    suffix = parse_input_suffix(input) or "none"
    size = "unknown" if input.content is None else str(len(input.content))
    return f"input={digest} suffix={suffix} size={size}"


def name_digest(name: str) -> str:
    """Return a short digest that names a file in logs without revealing its name.

    It is the first 12 hex characters of the SHA-256 of the name, so an operator can
    match a suspected file with ``sha256sum``.
    """
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:_NAME_DIGEST_CHARS]
