"""Ingestion and retrieval telemetry owned by one worker or reader process.

Metric labels stay low-cardinality (stage, outcome, connector type, parser family), and
nothing recorded here carries document content, titles or file names.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Literal

from opentelemetry import trace
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

from harborrag_adapters.connectors.policies.network import ResponseObservation
from harborrag_adapters.models.runtime import (
    LangfuseTelemetry,
    ModelClientConfig,
    OpenTelemetryTelemetry,
    TelemetryDispatcher,
)
from harborrag_engine.retrieval.service.contracts import RetrievalStep

from .connector_metrics import connector_metric_label
from .discovery_metrics import DiscoveryMetrics
from .observability_types import (
    ArtifactMetricKind,
    ChunkMetricKind,
    DocumentMetricOutcome,
    IngestionStage,
)
from .span_failures import record_failure
from .stage_observation import StageObservation

logger = logging.getLogger("harborrag.runtime.ingestion.observability")
SCHEDULED_RUN_OUTCOMES = ("started", "completed", "partial", "skipped", "cancelled", "failed")
PARSE_DURATION_BUCKETS_SECONDS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)
NO_RESPONSE_STATUS = "error"
RATE_LIMITED_STATUS_CODE = 429
FIRST_ERROR_STATUS_CODE = 400
CONNECTOR_RESPONSE_EVENT = "connector.response"

SubprocessOutcomeLabel = Literal[
    "success", "serialization_fail", "result_serialization_fail", "crash"
]


class IngestionTelemetry:
    """Own low-cardinality ingestion metrics and OpenTelemetry stage spans.

    Args:
        metrics_port: Port to serve ``/metrics`` on. None serves nothing.
        metrics_bind_address: Address the metrics server binds to.
        registry: Registry the metrics live in. A new one when omitted.
        tracer: Tracer for stage spans. The global ``harborrag.ingestion`` tracer when omitted.
    """

    def __init__(
        self,
        *,
        metrics_port: int | None = None,
        metrics_bind_address: str = "0.0.0.0",
        registry: CollectorRegistry | None = None,
        tracer: trace.Tracer | None = None,
    ) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self._tracer = tracer or trace.get_tracer("harborrag.ingestion")
        self._retrieval_tracer = trace.get_tracer("harborrag.retrieval")
        self._metrics_port = metrics_port
        self._metrics_bind_address = metrics_bind_address
        self._metrics_server: Any | None = None
        self._metrics_thread: Any | None = None
        self._stage_operations = Counter(
            "harborrag_ingestion_stage_operations_total",
            "Ingestion stage executions.",
            ("stage", "outcome"),
            registry=self.registry,
        )
        self._stage_duration = Histogram(
            "harborrag_ingestion_stage_duration_seconds",
            "Ingestion stage duration.",
            ("stage",),
            registry=self.registry,
        )
        self._documents = Counter(
            "harborrag_ingestion_documents_total",
            "Documents observed by the ingestion pipeline.",
            ("outcome", "connector_type"),
            registry=self.registry,
        )
        self._artifact_bytes = Counter(
            "harborrag_ingestion_artifact_bytes_total",
            "Bytes persisted as durable ingestion artifacts.",
            ("kind",),
            registry=self.registry,
        )
        self._chunks = Counter(
            "harborrag_ingestion_chunks_total",
            "Canonical chunks produced by kind.",
            ("kind",),
            registry=self.registry,
        )
        self._chunk_tokens = Counter(
            "harborrag_ingestion_chunk_tokens_total",
            "Tokens represented by accepted canonical chunks.",
            registry=self.registry,
        )
        self._verification_failures = Counter(
            "harborrag_ingestion_projection_verification_failures_total",
            "Projection verification failures.",
            registry=self.registry,
        )
        self._activity_retries = Counter(
            "harborrag_ingestion_temporal_activity_retries_total",
            "Temporal activity executions after the first attempt.",
            ("stage",),
            registry=self.registry,
        )
        self._rate_limit_events = Counter(
            "harborrag_ingestion_connector_rate_limit_events_total",
            "Connector requests delayed by a rate limiter.",
            ("connector_type",),
            registry=self.registry,
        )
        self._rate_limit_wait = Histogram(
            "harborrag_ingestion_connector_rate_limit_wait_seconds",
            "Time connector requests spend waiting for a rate-limit token.",
            ("connector_type",),
            registry=self.registry,
        )
        self._discovery = DiscoveryMetrics(self.registry)
        self._cleanup_backlog = Gauge(
            "harborrag_ingestion_cleanup_backlog",
            "Projection cleanup jobs left unresolved by the latest batch.",
            registry=self.registry,
        )
        self._retention_versions = Counter(
            "harborrag_retention_purge_versions_total",
            "Retired document versions handled by the retention purge, by outcome.",
            ("outcome",),
            registry=self.registry,
        )
        self._retention_deleted_objects = Counter(
            "harborrag_retention_purge_deleted_objects_total",
            "Object-store objects deleted by the retention purge.",
            registry=self.registry,
        )
        self._retention_backlog = Gauge(
            "harborrag_retention_purge_backlog",
            "Retired versions still eligible for purging when the latest sweep stopped.",
            registry=self.registry,
        )
        self._stale_rejections = Counter(
            "harborrag_retrieval_stale_candidates_rejected_total",
            "Stale vector candidates rejected by Postgres active-version validation.",
            registry=self.registry,
        )
        self._temporal_queue_depth = Gauge(
            "harborrag_temporal_task_queue_depth",
            "Best-effort Temporal backlog depth by task queue.",
            ("task_queue",),
            registry=self.registry,
        )
        self._temporal_worker_slots = Gauge(
            "harborrag_temporal_worker_slots",
            "Configured Temporal worker activity slots per task queue.",
            ("task_queue",),
            registry=self.registry,
        )
        self._temporal_worker_slot_saturation = Gauge(
            "harborrag_temporal_worker_slot_saturation",
            "Best-effort activity slot saturation ratio by task queue.",
            ("task_queue",),
            registry=self.registry,
        )
        self._scheduled_runs = Counter(
            "harborrag_temporal_scheduled_runs_total",
            "Scheduled workflow runs by outcome.",
            ("schedule_id", "outcome"),
            registry=self.registry,
        )
        self._schedule_to_start = Histogram(
            "harborrag_temporal_schedule_to_start_seconds",
            "Time between a scheduled action and its workflow start.",
            ("schedule_id",),
            registry=self.registry,
            buckets=(0.1, 0.5, 1, 5, 15, 60, 300, 900),
        )
        self._schedule_paused = Gauge(
            "harborrag_temporal_schedule_paused",
            "Whether a HarborRAG schedule is currently paused.",
            ("schedule_id",),
            registry=self.registry,
        )
        self._schedule_expected_paused = Gauge(
            "harborrag_temporal_schedule_expected_paused",
            "Whether a HarborRAG schedule is configured to be paused.",
            ("schedule_id",),
            registry=self.registry,
        )
        self._schedule_skipped_overlap = Gauge(
            "harborrag_temporal_schedule_actions_skipped_overlap",
            "Cumulative schedule actions skipped because of overlap.",
            ("schedule_id",),
            registry=self.registry,
        )
        self._schedule_ids: set[str] = set()
        self._seeded_schedule_ids: set[str] = set()
        self._subprocess_executions = Counter(
            "harborrag_ingestion_subprocess_executions_total",
            "Subprocess execution outcomes for CPU-intensive activities.",
            ("stage", "outcome"),
            registry=self.registry,
        )
        self._connector_requests = Counter(
            "harborrag_ingestion_connector_requests_total",
            "Connector HTTP requests by status class; 429 is counted on its own.",
            ("connector_type", "status"),
            registry=self.registry,
        )
        self._parse_duration = Histogram(
            "harborrag_ingestion_parse_duration_seconds",
            "Time one parser family spends parsing one document.",
            ("parser",),
            registry=self.registry,
            buckets=PARSE_DURATION_BUCKETS_SECONDS,
        )

    async def start(self) -> None:
        """Expose this process's registry when a metrics port is configured."""
        if self._metrics_port is None or self._metrics_server is not None:
            return
        self._metrics_server, self._metrics_thread = start_http_server(
            self._metrics_port,
            addr=self._metrics_bind_address,
            registry=self.registry,
        )

    async def close(self) -> None:
        """Stop the owned metrics server without owning global OTel providers."""
        server = self._metrics_server
        thread = self._metrics_thread
        self._metrics_server = None
        self._metrics_thread = None
        if server is None:
            return
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        if thread is not None:
            await asyncio.to_thread(thread.join, 2.0)

    def stage(
        self,
        stage: IngestionStage,
        *,
        attempt: int = 1,
        attributes: Mapping[str, str | int | float] | None = None,
    ) -> StageObservation:
        """Return a context that times and traces one execution of ``stage``."""
        return StageObservation(
            telemetry=self,
            stage=stage,
            attempt=attempt,
            attributes=attributes or {},
        )

    def record_documents(
        self,
        outcome: DocumentMetricOutcome,
        connector_type: str,
        count: int = 1,
    ) -> None:
        """Count documents that reached ``outcome`` for one connector type."""
        if count > 0:
            self._documents.labels(
                outcome=outcome.value,
                connector_type=connector_metric_label(connector_type),
            ).inc(count)

    def record_artifact_bytes(self, kind: ArtifactMetricKind, byte_size: int) -> None:
        """Add the bytes of one persisted artifact of ``kind``."""
        if byte_size > 0:
            self._artifact_bytes.labels(kind=kind.value).inc(byte_size)

    def record_chunks(self, kind: ChunkMetricKind, count: int) -> None:
        """Add ``count`` canonical chunks of ``kind``."""
        if count > 0:
            self._chunks.labels(kind=kind.value).inc(count)

    def record_chunk_tokens(self, count: int) -> None:
        """Add the tokens of accepted canonical chunks."""
        if count > 0:
            self._chunk_tokens.inc(count)

    def record_activity_retry(self, stage: IngestionStage) -> None:
        """Count one activity attempt after the first for ``stage``."""
        self._activity_retries.labels(stage=stage.value).inc()

    def record_verification_failure(self) -> None:
        """Count one projection verification failure."""
        self._verification_failures.inc()

    def record_cleanup_backlog(self, count: int) -> None:
        """Set the projection cleanup jobs the latest batch left unresolved."""
        self._cleanup_backlog.set(max(0, count))

    def record_retention_purge(
        self,
        *,
        purged: int,
        skipped: int,
        failed: int,
        deleted_objects: int,
        backlog: int | None = None,
    ) -> None:
        """Count one purge batch; only a full sweep knows the backlog."""
        for outcome, count in (("purged", purged), ("skipped", skipped), ("failed", failed)):
            if count > 0:
                self._retention_versions.labels(outcome=outcome).inc(count)
        if deleted_objects > 0:
            self._retention_deleted_objects.inc(deleted_objects)
        if backlog is not None:
            self._retention_backlog.set(max(0, backlog))

    def record_stale_candidate_rejections(self, count: int) -> None:
        """Count retrieval candidates dropped because their version is no longer active."""
        if count > 0:
            self._stale_rejections.inc(count)

    def retrieval_step(self, step: RetrievalStep) -> AbstractContextManager[object]:
        """Trace one retrieval step as the ``retrieval.<step>`` span.

        A step that raises marks its span with the cause's type and code, never the
        exception's text.

        Args:
            step: The step the ``with`` block runs.

        Returns:
            The span's context manager. Spans opened inside it become its children.
        """
        return _failure_recording_span(self._retrieval_tracer, f"retrieval.{step.value}")

    def record_connector_response(self, observation: ResponseObservation) -> None:
        """Count one connector HTTP request by its status class; trace the ones that failed.

        A response outside 2xx, or a request that got none, becomes a
        ``connector.response`` event on the active span with the connector, method,
        host and status, so the trace names the exact request that failed.

        Args:
            observation: The request's connector, method, host and status.
        """
        self._connector_requests.labels(
            connector_type=connector_metric_label(observation.connector),
            status=_status_label(observation.status_code),
        ).inc()
        if observation.status_code is None or observation.status_code >= FIRST_ERROR_STATUS_CODE:
            trace.get_current_span().add_event(
                CONNECTOR_RESPONSE_EVENT,
                {
                    "harborrag.connector": observation.connector,
                    "http.request.method": observation.method,
                    "server.address": observation.host,
                    "http.response.status_code": observation.status_code or 0,
                },
            )

    def record_parse(self, parser: str, duration_seconds: float) -> None:
        """Record how long one parser family took to parse one document.

        Args:
            parser: The parser family that produced the document.
            duration_seconds: Wall time of the parse.
        """
        self._parse_duration.labels(parser=parser).observe(max(0.0, duration_seconds))

    def record_temporal_queue_depth(self, task_queue: str, depth: int | None) -> None:
        """Set a task queue's backlog depth, or NaN when it is unknown."""
        value = float(depth) if depth is not None else float("nan")
        self._temporal_queue_depth.labels(task_queue=task_queue).set(value)

    def record_temporal_worker_slots(self, task_queue: str, slots: int) -> None:
        """Set the activity slots configured for a task queue."""
        self._temporal_worker_slots.labels(task_queue=task_queue).set(max(1, slots))

    def record_temporal_worker_slot_saturation(
        self,
        task_queue: str,
        *,
        slots: int,
        depth: int | None,
    ) -> None:
        """Set the share of a task queue's slots its backlog would fill."""
        if depth is None:
            value = float("nan")
        else:
            normalized_slots = max(1, slots)
            value = min(1.0, max(0.0, depth / normalized_slots))
        self._temporal_worker_slot_saturation.labels(task_queue=task_queue).set(value)

    def record_scheduled_run(
        self,
        schedule_id: str,
        outcome: str,
        *,
        schedule_to_start_seconds: float | None = None,
    ) -> None:
        """Count one scheduled run's outcome and its schedule-to-start delay."""
        self._seed_scheduled_runs(schedule_id)
        self._scheduled_runs.labels(schedule_id=schedule_id, outcome=outcome).inc()
        if schedule_to_start_seconds is not None and schedule_to_start_seconds >= 0:
            self._schedule_to_start.labels(schedule_id=schedule_id).observe(
                schedule_to_start_seconds
            )

    def _seed_scheduled_runs(self, schedule_id: str) -> None:
        """Create every outcome series of a schedule at 0, once.

        ``increase()`` ignores a series' first sample, so a run's first failure
        never alerts unless its series already exists. Every run records
        ``started`` first, which seeds the rest in time.
        """
        if schedule_id in self._seeded_schedule_ids:
            return
        for outcome in SCHEDULED_RUN_OUTCOMES:
            self._scheduled_runs.labels(schedule_id=schedule_id, outcome=outcome)
        self._seeded_schedule_ids.add(schedule_id)

    def record_schedule_states(
        self,
        states: Mapping[str, tuple[bool, bool, int]],
    ) -> None:
        """Set each schedule's paused, expected-paused and skipped-overlap gauges."""
        current_ids = set(states)
        for schedule_id in self._schedule_ids - current_ids:
            self._schedule_paused.remove(schedule_id)
            self._schedule_expected_paused.remove(schedule_id)
            self._schedule_skipped_overlap.remove(schedule_id)
        for schedule_id, (paused, expected_paused, skipped_overlap) in states.items():
            self._seed_scheduled_runs(schedule_id)
            self._schedule_paused.labels(schedule_id=schedule_id).set(int(paused))
            self._schedule_expected_paused.labels(schedule_id=schedule_id).set(int(expected_paused))
            self._schedule_skipped_overlap.labels(schedule_id=schedule_id).set(
                max(0, skipped_overlap)
            )
        self._schedule_ids = current_ids

    def record_rate_limit_wait(
        self,
        connector_type: str,
        wait_seconds: float,
    ) -> None:
        """Count one rate-limited connector request and how long it waited."""
        if wait_seconds <= 0:
            return
        label = connector_metric_label(connector_type)
        self._rate_limit_events.labels(connector_type=label).inc()
        self._rate_limit_wait.labels(connector_type=label).observe(wait_seconds)

    def record_discovery_page(
        self,
        connector_type: str,
        *,
        root_count: int,
        duration_seconds: float,
        replayed: bool,
    ) -> None:
        """Record one discovery page's root count and duration."""
        self._discovery.record(
            connector_metric_label(connector_type),
            root_count=root_count,
            duration_seconds=duration_seconds,
            replayed=replayed,
        )

    def record_subprocess_outcome(
        self,
        stage: IngestionStage,
        outcome: SubprocessOutcomeLabel,
    ) -> None:
        """Count how one CPU-bound subprocess execution ended."""
        self._subprocess_executions.labels(stage=stage.value, outcome=outcome).inc()

    def stage_tracer(self) -> trace.Tracer:
        """Return the tracer stage spans are opened with."""
        return self._tracer

    def record_stage(self, stage: IngestionStage, outcome: str, duration: float) -> None:
        """Count one stage execution by outcome and record its duration."""
        self._stage_operations.labels(stage=stage.value, outcome=outcome).inc()
        self._stage_duration.labels(stage=stage.value).observe(duration)


@contextmanager
def _failure_recording_span(tracer: trace.Tracer, name: str) -> Iterator[trace.Span]:
    """Open a span that records a failure as its cause's type and code, never its text."""
    with tracer.start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            yield span
        except Exception as error:
            record_failure(span, error)
            raise


def _status_label(status_code: int | None) -> str:
    """Return ``429``, a class such as ``5xx``, or ``error`` when nothing came back."""
    if status_code is None:
        return NO_RESPONSE_STATUS
    if status_code == RATE_LIMITED_STATUS_CODE:
        return str(status_code)
    return f"{status_code // 100}xx"


def build_model_telemetry(
    config: ModelClientConfig,
    *,
    langfuse_enabled: bool,
) -> TelemetryDispatcher:
    """Compose sanitized model telemetry for embedding or chat clients.

    Annotated against the shared client-config base so every model family --
    embed, chat -- composes its dispatcher the same way; only
    ``config.observability`` is read.
    """
    sinks: list[object] = [OpenTelemetryTelemetry()]
    if langfuse_enabled:
        sinks.append(LangfuseTelemetry())
    return TelemetryDispatcher(sinks, config=config.observability)
