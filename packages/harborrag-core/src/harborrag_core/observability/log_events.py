"""Structured log events: an event name plus safe fields, the same shape in every package.

Fields carry IDs, counts, codes, reasons and durations, never document text, titles,
document file names, URLs or exception messages. ``config_path`` names a deployment
config file, and ``server_address`` and ``server_port`` a deployment endpoint. A field
whose name is not on the safe list is dropped and named in ``dropped_fields``, so
content cannot reach a log by accident. The fields are written into the message, where
the console shows them, and onto the record, where the OpenTelemetry log exporter sends
them as attributes the collector keeps. An optional ``hint`` adds one fixed instruction
for the operator, written in the code.

Typical usage example:

    with log_context(task_id=task_id, connector="jira"):
        log_event(logger, "ingestion.discovery_completed", documents_count=12)
        log_failure(logger, "ingestion.activity_failed", error, stage="discovery")
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum, StrEnum
from typing import LiteralString

type LogValue = str | int | float | bool | None
type EventLogger = logging.Logger | logging.LoggerAdapter[logging.Logger]

EVENT_FIELD = "event"
DROPPED_FIELDS_FIELD = "dropped_fields"
ERROR_TYPE_FIELD = "error_type"
ERROR_CHAIN_FIELD = "error_chain"
ERROR_CODE_FIELD = "error_code"
STATUS_CODE_FIELD = "status_code"
RETRYABLE_FIELD = "retryable"
HINT_FIELD = "hint"
MAX_VALUE_LENGTH = 200
MAX_CHAIN_LENGTH = 5
SAFE_FIELD_SUFFIXES = (
    "_id",
    "_count",
    "_ms",
    "_seconds",
    "_bytes",
    "_code",
    "_type",
    "_reason",
    "_status",
    "_kind",
)
SAFE_FIELD_NAMES = frozenset(
    {
        "stage",
        "status",
        "outcome",
        "reason",
        "lane",
        "mode",
        "tool",
        "connector",
        "backend",
        "operation",
        "attempt",
        "retryable",
        "config_path",
        "server_address",
        "server_port",
        ERROR_CHAIN_FIELD,
        EVENT_FIELD,
        DROPPED_FIELDS_FIELD,
    }
)

_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PLAIN_VALUE = re.compile(r"[^\s\"=]+")
_RESERVED_RECORD_ATTRIBUTES = frozenset(
    {*logging.LogRecord("", 0, "", 0, "", None, None).__dict__, "message", "asctime"}
)
_STATUS_CODE_RANGE = range(100, 600)


class FailureKind(StrEnum):
    """Whose failure it is, which sets its level and whether it gets a traceback.

    Attributes:
        CALLER: The caller's mistake, such as a bad cursor; INFO, no traceback.
        EXPECTED: An outcome the code handles by design, such as an empty index or a
            disabled feature; WARNING, no traceback.
        CLASSIFIED: A failure with a known cause, such as a rejected credential or an
            unreachable store; ERROR, no traceback, because the code says why.
        UNEXPECTED: Anything else, such as a bug; ERROR, with the traceback the
            console prints.
    """

    CALLER = "caller"
    EXPECTED = "expected"
    CLASSIFIED = "classified"
    UNEXPECTED = "unexpected"


_FAILURE_LEVELS = {
    FailureKind.CALLER: logging.INFO,
    FailureKind.EXPECTED: logging.WARNING,
    FailureKind.CLASSIFIED: logging.ERROR,
    FailureKind.UNEXPECTED: logging.ERROR,
}
_context: ContextVar[tuple[tuple[str, LogValue], ...]] = ContextVar(
    "harborrag_log_context", default=()
)


def is_safe_field_name(name: str) -> bool:
    """Return whether ``name`` may carry a log field: an ID, count, code, reason or duration."""
    if not _FIELD_NAME.fullmatch(name) or name in _RESERVED_RECORD_ATTRIBUTES:
        return False
    return name in SAFE_FIELD_NAMES or name.endswith(SAFE_FIELD_SUFFIXES)


def log_event(
    logger: EventLogger,
    event: str,
    *,
    level: int = logging.INFO,
    hint: LiteralString | None = None,
    error: BaseException | None = None,
    **fields: object,
) -> None:
    """Write one structured event with the run context and the given fields.

    Args:
        logger: The module's logger, or a ``LoggerAdapter`` such as Temporal's
            replay-safe ``workflow.logger``.
        event: Dotted lower-case event name, such as ``mcp.tool_failed``.
        level: ``logging`` level of the record.
        hint: One fixed instruction for the operator, such as which setting to change.
            It is written in the code and never holds runtime data.
        error: An exception the event is about, described as ``failure_fields`` does.
            Use it for a failure whose level no ``FailureKind`` fits; it never adds a
            traceback.
        **fields: IDs, counts, codes, reasons and durations. A field with an unsafe
            name, or a value that is not a short scalar, is dropped and listed.
    """
    if logger.isEnabledFor(level):
        described = {**failure_fields(error), **fields} if error is not None else fields
        _emit(logger, level, _record(event, described, hint), traceback=None, stacklevel=1)


def log_failure(
    logger: EventLogger,
    event: str,
    error: BaseException,
    kind: FailureKind = FailureKind.UNEXPECTED,
    stacklevel: int = 1,
    **fields: object,
) -> None:
    """Write one failure event that names its cause without the exception's text.

    The record carries the root cause's type, the chain of cause types, an error code,
    an HTTP status and the retryable flag where the error knows them (see
    ``failure_fields``). A caller that has classified the failure passes ``error_code``
    or ``retryable`` as fields, which win. Only an unexpected failure carries the
    traceback, which only the console prints.

    Args:
        logger: The module's logger, or a ``LoggerAdapter`` such as ``workflow.logger``.
        event: Dotted lower-case event name, such as ``ingestion.activity_failed``.
        error: The exception that caused the failure.
        kind: Whose failure it is, which sets the level and the traceback.
        stacklevel: As for ``logging``: 2 makes a helper that wraps this call report
            its own caller's file and line.
        **fields: IDs, counts, codes, reasons and durations, as for ``log_event``.
    """
    level = _FAILURE_LEVELS[kind]
    if logger.isEnabledFor(level):
        record = _record(event, {**failure_fields(error), **fields})
        traceback = error if kind is FailureKind.UNEXPECTED else None
        _emit(logger, level, record, traceback=traceback, stacklevel=stacklevel)


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """Add fields to every event written inside the block, such as a run's IDs.

    Contexts nest; a field set by an inner context or passed to an event wins.

    Args:
        **fields: IDs, counts, codes and reasons, checked as for ``log_event``.

    Yields:
        None: the fields apply until the block ends.
    """
    accepted, _ = _safe_fields(fields)
    merged = {**dict(_context.get()), **accepted}
    token = _context.set(tuple(merged.items()))
    try:
        yield
    finally:
        _context.reset(token)


def failure_fields(
    error: BaseException,
    *,
    code: str | None = None,
    retryable: bool | None = None,
) -> dict[str, LogValue]:
    """Describe an exception with safe fields: types, code, HTTP status, retryable.

    Causes are followed through ``__cause__``, and through ``__context__`` unless it was
    suppressed, so a wrapper such as a Temporal failure still names the error behind it.
    ``code``, ``status_code`` and ``retryable`` come from the first error in the chain
    that carries them as attributes, unless the caller passes them.

    Args:
        error: The exception to describe.
        code: The failure's code, which overrides one found on the errors.
        retryable: Whether a retry can succeed, which overrides the errors' own flag.

    Returns:
        ``error_type`` (the root cause), ``error_chain`` (outermost first) when there is
        more than one error, and ``error_code``, ``status_code`` and ``retryable`` when known.
    """
    chain = _cause_chain(error)
    described: dict[str, LogValue] = {ERROR_TYPE_FIELD: type(chain[-1]).__name__}
    if len(chain) > 1:
        described[ERROR_CHAIN_FIELD] = ">".join(type(item).__name__ for item in chain)
    found_code = code if code is not None else _first_attribute(chain, ("error_code", "code"), str)
    if found_code:
        described[ERROR_CODE_FIELD] = found_code
    status_code = _first_attribute(chain, ("status_code",), int)
    if isinstance(status_code, int) and status_code in _STATUS_CODE_RANGE:
        described[STATUS_CODE_FIELD] = status_code
    found_retryable = (
        retryable if retryable is not None else _first_attribute(chain, ("retryable",), bool)
    )
    if isinstance(found_retryable, bool):
        described[RETRYABLE_FIELD] = found_retryable
    return described


def root_cause(error: BaseException) -> BaseException:
    """Return the error behind ``error``'s chain of causes, as ``failure_fields`` follows it.

    Args:
        error: The exception as raised.

    Returns:
        The last error in the chain, or ``error`` itself when nothing caused it.
    """
    return _cause_chain(error)[-1]


def _record(
    event: str, fields: Mapping[str, object], hint: str | None = None
) -> tuple[str, dict[str, LogValue]]:
    """Merge the run context, drop unsafe fields, and return the message and record fields.

    The hint is written into the message only; it is fixed text, not a field to query.
    """
    accepted, dropped = _safe_fields(fields)
    merged: dict[str, LogValue] = {**dict(_context.get()), **accepted}
    if dropped:
        merged[DROPPED_FIELDS_FIELD] = ",".join(sorted(dropped))
    parts = [event, *(f"{name}={_render(value)}" for name, value in merged.items())]
    if hint:
        parts.append(f"{HINT_FIELD}={_render(hint)}")
    message = " ".join(parts)
    return message, {EVENT_FIELD: event, **merged}


def _emit(
    logger: EventLogger,
    level: int,
    record: tuple[str, dict[str, LogValue]],
    *,
    traceback: BaseException | None,
    stacklevel: int,
) -> None:
    """Write the record, keeping the fields when the logger is an adapter.

    A standard ``LoggerAdapter`` replaces the caller's ``extra`` with its own, so the
    adapter's ``process`` runs first (Temporal's adds the workflow's details there) and
    the fields are merged back before the wrapped logger writes the record.
    ``stacklevel`` 1 reports the caller of the public helper that called this.
    """
    message, extra = record
    if isinstance(logger, logging.LoggerAdapter):
        message, extra = _through_adapter(logger, message, extra)
        logger = logger.logger
    logger.log(level, message, exc_info=traceback, extra=extra, stacklevel=stacklevel + 2)


def _through_adapter(
    adapter: logging.LoggerAdapter[logging.Logger], message: str, extra: dict[str, LogValue]
) -> tuple[str, dict[str, LogValue]]:
    """Let the adapter shape the message and add its fields, keeping the event's fields."""
    shaped, options = adapter.process(message, {"extra": extra})
    added = options.get("extra")
    merged = dict(added) if isinstance(added, Mapping) else {}
    return str(shaped), {**merged, **extra}


def _safe_fields(fields: Mapping[str, object]) -> tuple[dict[str, LogValue], list[str]]:
    """Split fields into the ones safe to log and the names of the rest."""
    accepted: dict[str, LogValue] = {}
    dropped: list[str] = []
    for name, value in fields.items():
        scalar = _scalar(value)
        if is_safe_field_name(name) and not isinstance(scalar, _Unloggable):
            accepted[name] = scalar
        else:
            dropped.append(name if _FIELD_NAME.fullmatch(name) else "invalid_name")
    return accepted, dropped


class _Unloggable:
    """A value that is not a short scalar, so it never reaches a log."""


def _scalar(value: object) -> LogValue | _Unloggable:
    """Return a loggable scalar for ``value``, or ``_Unloggable`` for anything else."""
    if isinstance(value, Enum):
        value = value.value
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value if len(value) <= MAX_VALUE_LENGTH else value[: MAX_VALUE_LENGTH - 1] + "…"
    return _Unloggable()


def _render(value: LogValue) -> str:
    """Render a value for the message: bare when plain, JSON-quoted otherwise."""
    text = (
        "true"
        if value is True
        else "false"
        if value is False
        else ""
        if value is None
        else str(value)
    )
    return text if _PLAIN_VALUE.fullmatch(text) else json.dumps(text)


def _cause_chain(error: BaseException) -> list[BaseException]:
    """Return the error and its causes, outermost first, at most ``MAX_CHAIN_LENGTH``."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain and len(chain) < MAX_CHAIN_LENGTH:
        chain.append(current)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return chain


def _first_attribute[T](
    chain: list[BaseException], names: tuple[str, ...], kind: type[T]
) -> T | None:
    """Return the first value of one of ``names`` that has type ``kind`` along the chain."""
    for item in chain:
        for name in names:
            value = getattr(item, name, None)
            if isinstance(value, kind) and not (kind is int and isinstance(value, bool)):
                return value
    return None
