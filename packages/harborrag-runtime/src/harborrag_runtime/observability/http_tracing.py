"""Server spans for HTTP requests, named by route template, never by raw path.

The span continues a trace the caller started with a W3C ``traceparent`` header. Only
the method, the route template and the status code are recorded, never the query
string or the body.

Typical usage example:

    with server_span("POST", "/v1/chat/completions", request.headers) as span:
        response = await call_next(request)
        span.finish(response.status_code)
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

_SERVER_ERROR = 500


class ServerSpan:
    """The span of one HTTP request, finished with the response status.

    Args:
        span: The OpenTelemetry span this request records into.
    """

    def __init__(self, span: trace.Span) -> None:
        self._span = span

    def finish(self, status_code: int) -> None:
        """Record the response status; a 5xx marks the span as failed."""
        self._span.set_attribute("http.response.status_code", status_code)
        if status_code >= _SERVER_ERROR:
            self._span.set_status(Status(StatusCode.ERROR))


@contextmanager
def server_span(method: str, route: str, headers: Mapping[str, str]) -> Iterator[ServerSpan]:
    """Trace one HTTP request as the current span while the block runs.

    Args:
        method: The request method, upper case.
        route: The matched route template, such as ``/v1/sources/{source_id}``.
        headers: The request headers, read for a ``traceparent`` to continue.

    Yields:
        The request's span, to finish with the response status.
    """
    tracer = trace.get_tracer("harborrag.http")
    with tracer.start_as_current_span(
        f"{method} {route}",
        context=propagate.extract(headers),
        kind=SpanKind.SERVER,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        span.set_attribute("http.request.method", method)
        span.set_attribute("http.route", route)
        yield ServerSpan(span)
