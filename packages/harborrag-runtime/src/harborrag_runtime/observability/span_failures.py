"""Mark a span failed with its cause's type, code and HTTP status, never the exception's text.

OpenTelemetry's default puts the exception message in the span status and a stack trace
in an exception event. The collector filters attributes but not status descriptions, so
spans HarborRAG opens turn that default off and call ``record_failure`` instead. The
attributes come from the same ``failure_fields`` the failure log line uses, so a trace
and its log describe a failure alike.

Typical usage example:

    with tracer.start_as_current_span("x", record_exception=False, set_status_on_exception=False) as span:
        try:
            ...
        except Exception as error:
            record_failure(span, error)
            raise
"""

from __future__ import annotations

from opentelemetry.trace import Span, Status, StatusCode

from harborrag_core.observability.log_events import (
    ERROR_CHAIN_FIELD,
    ERROR_CODE_FIELD,
    ERROR_TYPE_FIELD,
    RETRYABLE_FIELD,
    STATUS_CODE_FIELD,
    failure_fields,
    root_cause,
)

ERROR_TYPE_ATTRIBUTE = "error.type"
ERROR_CODE_ATTRIBUTE = "harborrag.error.code"
ERROR_CHAIN_ATTRIBUTE = "harborrag.error.chain"
RETRYABLE_ATTRIBUTE = "harborrag.error.retryable"
HTTP_STATUS_ATTRIBUTE = "http.response.status_code"
EXCEPTION_EVENT = "exception"
EXCEPTION_TYPE_ATTRIBUTE = "exception.type"


def record_failure(
    span: Span,
    error: BaseException,
    *,
    code: str | None = None,
    retryable: bool | None = None,
) -> None:
    """Mark ``span`` failed and record why: root cause type, cause chain, code, HTTP status.

    The status description is ``<root cause type>`` or ``<root cause type>: <code>``,
    and the exception event names the root cause's qualified type only.

    Args:
        span: The span the failure happened in.
        error: The exception, as raised; wrappers are followed to the root cause.
        code: The failure's safe code, when the caller has classified it.
        retryable: Whether a retry can succeed, when the caller knows.
    """
    fields = failure_fields(error, code=code, retryable=retryable)
    root_type = str(fields[ERROR_TYPE_FIELD])
    found_code = fields.get(ERROR_CODE_FIELD)
    span.set_attribute(ERROR_TYPE_ATTRIBUTE, root_type)
    for field, attribute in (
        (ERROR_CODE_FIELD, ERROR_CODE_ATTRIBUTE),
        (ERROR_CHAIN_FIELD, ERROR_CHAIN_ATTRIBUTE),
        (RETRYABLE_FIELD, RETRYABLE_ATTRIBUTE),
        (STATUS_CODE_FIELD, HTTP_STATUS_ATTRIBUTE),
    ):
        value = fields.get(field)
        if value is not None:
            span.set_attribute(attribute, value)
    span.add_event(EXCEPTION_EVENT, {EXCEPTION_TYPE_ATTRIBUTE: _qualified_root_type(error)})
    description = f"{root_type}: {found_code}" if found_code else root_type
    span.set_status(Status(StatusCode.ERROR, description))


def record_failure_code(span: Span, code: str) -> None:
    """Mark ``span`` failed for a failure that raised nothing, such as a tool's ``ok: false``.

    Args:
        span: The span the failure happened in.
        code: The failure's safe code.
    """
    span.set_attribute(ERROR_CODE_ATTRIBUTE, code)
    span.set_status(Status(StatusCode.ERROR, code))


def _qualified_root_type(error: BaseException) -> str:
    """Return ``module.Type`` of the error's root cause, as an exception event names it."""
    root = type(root_cause(error))
    return f"{root.__module__}.{root.__qualname__}"
