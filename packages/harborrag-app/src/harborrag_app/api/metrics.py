"""Prometheus metrics and request spans owned by one API application instance.

Metrics and spans name a request by its route template, never its raw path, so
resource IDs stay out of label values and span names.
"""

from __future__ import annotations

from contextlib import nullcontext
from time import perf_counter

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.gc_collector import GCCollector
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from harborrag_runtime.observability.http_tracing import ServerSpan, server_span

UNMATCHED_ROUTE = "unmatched"
UNTRACED_ROUTES = frozenset({"/healthz", "/readyz", UNMATCHED_ROUTE})


class ApiMetrics:
    """API and process metrics isolated from the module-global registry.

    Args:
        version: The API release, exported as ``harborrag_api_info``.
    """

    def __init__(self, *, version: str) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        GCCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)
        ProcessCollector(registry=self.registry)

        self._info = Gauge(
            "harborrag_api_info",
            "HarborRAG API build information.",
            ("version",),
            registry=self.registry,
        )
        self._requests = Counter(
            "harborrag_api_http_requests_total",
            "Completed HarborRAG API HTTP requests.",
            ("method", "route", "status_code"),
            registry=self.registry,
        )
        self._duration = Histogram(
            "harborrag_api_http_request_duration_seconds",
            "HarborRAG API HTTP request duration in seconds.",
            ("method", "route"),
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
        )
        self._in_progress = Gauge(
            "harborrag_api_http_requests_in_progress",
            "HarborRAG API HTTP requests currently being served.",
            ("method",),
            registry=self.registry,
        )
        self._info.labels(version=version).set(1)

    def started(self, method: str) -> None:
        """Count one request as in progress."""
        self._in_progress.labels(method=method).inc()

    def completed(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        """Count one finished request and record its duration.

        Args:
            method: The request method, upper case.
            route: The matched route template, or ``unmatched``.
            status_code: The response status.
            duration_seconds: Time from receiving the request to its response.
        """
        self._in_progress.labels(method=method).dec()
        self._requests.labels(
            method=method,
            route=route,
            status_code=str(status_code),
        ).inc()
        self._duration.labels(method=method, route=route).observe(duration_seconds)

    def render(self) -> bytes:
        """Return the registry in the Prometheus text exposition format."""
        return generate_latest(self.registry)


class ApiMetricsMiddleware(BaseHTTPMiddleware):
    """Record request metrics and a server span, named by route template.

    Liveness and readiness probes, and paths that match no route, such as the
    ops port's ``/metrics`` scrape, are counted but not traced.

    Args:
        app: The application this middleware wraps.
        metrics: Where request metrics are recorded.
    """

    def __init__(self, app: ASGIApp, *, metrics: ApiMetrics) -> None:
        super().__init__(app)
        self._metrics = metrics

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Serve the request inside its span and record its metrics."""
        method = request.method.upper()
        route = _route_template(request, path=request.scope["path"])
        status_code = 500
        started_at = perf_counter()
        self._metrics.started(method)
        traced = (
            nullcontext(None)
            if route in UNTRACED_ROUTES
            else server_span(method, route, request.headers)
        )
        with traced as span:
            try:
                response = await call_next(request)
                status_code = response.status_code
                return response
            finally:
                _finish(span, status_code)
                self._metrics.completed(
                    method=method,
                    route=route,
                    status_code=status_code,
                    duration_seconds=perf_counter() - started_at,
                )


def _finish(span: ServerSpan | None, status_code: int) -> None:
    """Record the response status on the request's span, when it has one."""
    if span is not None:
        span.finish(status_code)


def _route_template(request: Request, *, path: str) -> str:
    """Resolve the registered route without emitting raw resource paths.

    ``app.state`` is untyped, so the template is coerced to ``str`` here rather
    than letting ``Any`` reach a metric label.
    """
    for template, pattern in request.app.state.api_metric_routes:
        if pattern.match(path):
            return str(template)
    return UNMATCHED_ROUTE
