"""Prometheus instrumentation owned by one API application instance."""

from __future__ import annotations

from time import perf_counter

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.gc_collector import GCCollector
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp


class ApiMetrics:
    """API and process metrics isolated from the module-global registry."""

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
        self._in_progress.labels(method=method).inc()

    def completed(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        self._in_progress.labels(method=method).dec()
        self._requests.labels(
            method=method,
            route=route,
            status_code=str(status_code),
        ).inc()
        self._duration.labels(method=method, route=route).observe(duration_seconds)

    def render(self) -> bytes:
        return generate_latest(self.registry)


class ApiMetricsMiddleware(BaseHTTPMiddleware):
    """Record request metrics with route templates instead of raw paths."""

    def __init__(self, app: ASGIApp, *, metrics: ApiMetrics) -> None:
        super().__init__(app)
        self._metrics = metrics

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        method = request.method.upper()
        request_path = request.scope["path"]
        status_code = 500
        started_at = perf_counter()
        self._metrics.started(method)
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            self._metrics.completed(
                method=method,
                route=_route_template(request, path=request_path),
                status_code=status_code,
                duration_seconds=perf_counter() - started_at,
            )


def _route_template(request: Request, *, path: str) -> str:
    """Resolve the registered route without emitting raw resource paths."""
    # app.state is untyped, so the template arrives as Any; coerce at the boundary rather
    # than letting Any escape into the metric label.
    for template, pattern in request.app.state.api_metric_routes:
        if pattern.match(path):
            return str(template)
    return "unmatched"
