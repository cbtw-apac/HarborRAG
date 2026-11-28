"""Prometheus metrics and request spans owned by one API application instance.

Metrics and spans name a request by its route template, never its raw path, so
resource IDs stay out of label values and span names. Source gauges carry IDs only,
never a connection or source name.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from time import perf_counter

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.gc_collector import GCCollector
from prometheus_client.metrics_core import GaugeMetricFamily, Metric
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from harborrag_core.ports.source_freshness import SourceFreshness
from harborrag_runtime.observability.http_tracing import ServerSpan, server_span
from harborrag_runtime.observability.schedule_metrics import SCHEDULE_SLOT_METRICS

UNMATCHED_ROUTE = "unmatched"
UNTRACED_ROUTES = frozenset({"/healthz", "/readyz", UNMATCHED_ROUTE})
SOURCE_LAST_SUCCESS_METRIC = "harborrag_ingestion_source_last_success_timestamp_seconds"
SOURCE_LAST_RUN_STATUS_METRIC = "harborrag_ingestion_source_last_run_status"
SOURCE_STATE_REFRESH_METRIC = "harborrag_ingestion_source_state_refresh_timestamp_seconds"
_REPLICA_AGGREGATION = "Every API replica reports the same value; aggregate with max."
_SOURCE_LABELS = ("tenant", "source_scope_id", "connector_type")


class ApiMetrics:
    """API, process and source freshness metrics isolated from the module-global registry.

    The schedule slot counter is the process's own, because the slot worker runs in
    this process and counts into it.

    Args:
        version: The API release, exported as ``harborrag_api_info``.
    """

    def __init__(self, *, version: str) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        GCCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)
        ProcessCollector(registry=self.registry)
        self._source_freshness = _SourceFreshnessCollector()
        self.registry.register(self._source_freshness)
        self.registry.register(SCHEDULE_SLOT_METRICS)

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

    def record_source_freshness(
        self, sources: Sequence[SourceFreshness], *, refreshed_at: float
    ) -> None:
        """Replace every source gauge with one refresh's sources, all at once.

        A source missing from ``sources`` stops being exported. Until the first call no
        source gauge, refresh time included, has a sample.

        Args:
            sources: Every source the refresh read.
            refreshed_at: Unix time the refresh finished.
        """
        self._source_freshness.snapshot = _SourceFreshnessSnapshot(
            sources=tuple(sources), refreshed_at=refreshed_at
        )

    def render(self) -> bytes:
        """Return the registry in the Prometheus text exposition format."""
        return generate_latest(self.registry)


@dataclass(frozen=True, slots=True)
class _SourceFreshnessSnapshot:
    """The sources one successful refresh read.

    Attributes:
        sources: Every source the refresh read.
        refreshed_at: Unix time the refresh finished.
    """

    sources: tuple[SourceFreshness, ...]
    refreshed_at: float


class _SourceFreshnessCollector:
    """Yield the source gauges from the latest snapshot, which a refresh swaps whole.

    Attributes:
        snapshot: The latest successful refresh, or None before the first one.
    """

    snapshot: _SourceFreshnessSnapshot | None

    def __init__(self) -> None:
        self.snapshot = None

    def collect(self) -> Iterator[Metric]:
        """Yield the three source gauge families; without a snapshot they have no samples."""
        snapshot = self.snapshot
        last_success = GaugeMetricFamily(
            SOURCE_LAST_SUCCESS_METRIC,
            "Unix time the source's latest completed or partial ingestion run ended, "
            f"0 when none has. {_REPLICA_AGGREGATION}",
            labels=_SOURCE_LABELS,
        )
        last_run_status = GaugeMetricFamily(
            SOURCE_LAST_RUN_STATUS_METRIC,
            "State of the source's most recently submitted ingestion run in the status "
            f"label, always 1. {_REPLICA_AGGREGATION}",
            labels=(*_SOURCE_LABELS, "status"),
        )
        refresh = GaugeMetricFamily(
            SOURCE_STATE_REFRESH_METRIC,
            "Unix time this API replica last refreshed the source state gauges.",
        )
        if snapshot is not None:
            for source in snapshot.sources:
                labels = [source.tenant_id, source.source_scope_id, source.connector_type]
                last_success.add_metric(labels, _unix_time(source))
                last_run_status.add_metric([*labels, source.last_run_status.value.lower()], 1)
            refresh.add_metric([], snapshot.refreshed_at)
        yield from (last_success, last_run_status, refresh)


def _unix_time(source: SourceFreshness) -> float:
    """Return when the source last succeeded as Unix time, or 0 when it never has."""
    return source.last_success_at.timestamp() if source.last_success_at is not None else 0.0


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
