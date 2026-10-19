from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Literal

from opentelemetry import trace
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

from harborrag_adapters.models.runtime import (
    LangfuseTelemetry,
    ModelClientConfig,
    OpenTelemetryTelemetry,
    TelemetryDispatcher,
    TelemetryEvent,
)

from .connector_metrics import connector_metric_label, connector_metric_labels
from .discovery_metrics import DiscoveryMetrics
from .observability_types import (
    DOCUMENT_KIND_NONE,
    DOCUMENT_KINDS,
    ArtifactMetricKind,
    ChunkMetricKind,
    DocumentMetricOutcome,
    IngestionStage,
)
from .process_metrics import register_process_metrics
from .stage_observation import StageObservation

logger = logging.getLogger("harborrag.runtime.ingestion.observability")

# One document can spend seconds to tens of minutes between fetch and publish,
# well past prometheus_client's 10 s default ceiling.
DOCUMENT_DURATION_BUCKETS = (
    0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1200.0, 1800.0, 3600.0
)  # fmt: skip

SubprocessOutcomeLabel = Literal[
    "success", "serialization_fail", "result_serialization_fail", "crash"
]


# Stages that run once per document and so carry a document_kind.
DOCUMENT_STAGES = (
    IngestionStage.FETCH,
    IngestionStage.PARSE_NORMALIZE,
    IngestionStage.CONTENT_SYNC,
    IngestionStage.CANONICAL_PERSIST,
    IngestionStage.CHUNK,
    IngestionStage.ENCODE,
    IngestionStage.RELATION_BUILD,
    IngestionStage.PROJECTION_BUILD,
    IngestionStage.QDRANT_WRITE,
    IngestionStage.FALKORDB_WRITE,
    IngestionStage.VERIFICATION,
    IngestionStage.PUBLICATION,
)

_TERMINAL_DOCUMENT_OUTCOMES = (
    DocumentMetricOutcome.ACTIVATED,
    DocumentMetricOutcome.REPUBLISHED,
    DocumentMetricOutcome.SKIPPED,
    DocumentMetricOutcome.FAILED,
)


class IngestionTelemetry:
    """Own low-cardinality ingestion metrics and OpenTelemetry stage spans."""

    def __init__(
        self,
        *,
        metrics_port: int | None = None,
        metrics_bind_address: str = "0.0.0.0",
        registry: CollectorRegistry | None = None,
        tracer: Any | None = None,
    ) -> None:
        if registry is None:
            registry = CollectorRegistry(auto_describe=True)
            # Only a registry this telemetry owns gets process collectors, so a
            # caller-shared registry never sees them registered twice.
            register_process_metrics(registry)
        self.registry = registry
        self._tracer = tracer or trace.get_tracer("harborrag.ingestion")
        self._metrics_port = metrics_port
        self._metrics_bind_address = metrics_bind_address
        self._metrics_server: Any | None = None
        self._metrics_thread: Any | None = None
        self._stage_operations = Counter(
            "harborrag_ingestion_stage_operations_total",
            "Ingestion stage executions.",
            ("stage", "outcome", "document_kind"),
            registry=self.registry,
        )
        self._stage_duration = Histogram(
            "harborrag_ingestion_stage_duration_seconds",
            "Ingestion stage duration.",
            ("stage", "document_kind"),
            registry=self.registry,
        )
        self._documents = Counter(
            "harborrag_ingestion_documents_total",
            "Documents observed by the ingestion pipeline.",
            ("outcome", "connector_type"),
            registry=self.registry,
        )
        self._document_duration = Histogram(
            "harborrag_ingestion_document_duration_seconds",
            "End-to-end time for one document, from workflow start to its terminal outcome.",
            ("outcome", "connector_type"),
            buckets=DOCUMENT_DURATION_BUCKETS,
            registry=self.registry,
        )
        self._initialize_document_series()
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
        self._subprocess_executions = Counter(
            "harborrag_ingestion_subprocess_executions_total",
            "Subprocess execution outcomes for CPU-intensive activities.",
            ("stage", "outcome"),
            registry=self.registry,
        )
        self._parser_engine_attempts = Counter(
            "harborrag_ingestion_parser_engine_attempts_total",
            "PDF parser engine attempts within the parse_normalize stage.",
            ("engine", "outcome"),
            registry=self.registry,
        )
        self._parser_engine_duration = Histogram(
            "harborrag_ingestion_parser_engine_duration_seconds",
            "PDF parser engine attempt duration, one observation per engine tried.",
            ("engine",),
            registry=self.registry,
        )

    def _initialize_document_series(self) -> None:
        """Create every outcome/connector series at zero.

        ``increase()`` and ``rate()`` use a series' first scraped sample as
        the baseline, so a series born on its first increment silently loses
        that increment. Both label sets are bounded, so pre-creating them is cheap.
        """

        connectors = connector_metric_labels()
        for outcome in DocumentMetricOutcome:
            for connector in connectors:
                self._documents.labels(outcome=outcome.value, connector_type=connector)
        for outcome in _TERMINAL_DOCUMENT_OUTCOMES:
            for connector in connectors:
                self._document_duration.labels(outcome=outcome.value, connector_type=connector)
        for stage in DOCUMENT_STAGES:
            for kind in DOCUMENT_KINDS:
                self._stage_duration.labels(stage=stage.value, document_kind=kind)
                for outcome in ("succeeded", "failed"):
                    self._stage_operations.labels(
                        stage=stage.value, outcome=outcome, document_kind=kind
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
        if count > 0:
            self._documents.labels(
                outcome=outcome.value,
                connector_type=connector_metric_label(connector_type),
            ).inc(count)

    def record_document_duration(
        self,
        outcome: DocumentMetricOutcome,
        connector_type: str,
        duration_seconds: float,
    ) -> None:
        self._document_duration.labels(
            outcome=outcome.value,
            connector_type=connector_metric_label(connector_type),
        ).observe(max(0.0, duration_seconds))

    def record_artifact_bytes(self, kind: ArtifactMetricKind, byte_size: int) -> None:
        if byte_size > 0:
            self._artifact_bytes.labels(kind=kind.value).inc(byte_size)

    def record_chunks(self, kind: ChunkMetricKind, count: int) -> None:
        if count > 0:
            self._chunks.labels(kind=kind.value).inc(count)

    def record_chunk_tokens(self, count: int) -> None:
        if count > 0:
            self._chunk_tokens.inc(count)

    def record_activity_retry(self, stage: IngestionStage) -> None:
        self._activity_retries.labels(stage=stage.value).inc()

    def record_verification_failure(self) -> None:
        self._verification_failures.inc()

    def record_cleanup_backlog(self, count: int) -> None:
        self._cleanup_backlog.set(max(0, count))

    def record_stale_candidate_rejections(self, count: int) -> None:
        if count > 0:
            self._stale_rejections.inc(count)

    def record_temporal_queue_depth(self, task_queue: str, depth: int | None) -> None:
        value = float(depth) if depth is not None else float("nan")
        self._temporal_queue_depth.labels(task_queue=task_queue).set(value)

    def record_temporal_worker_slots(self, task_queue: str, slots: int) -> None:
        self._temporal_worker_slots.labels(task_queue=task_queue).set(max(1, slots))

    def record_temporal_worker_slot_saturation(
        self,
        task_queue: str,
        *,
        slots: int,
        depth: int | None,
    ) -> None:
        if depth is None:
            value = float("nan")
        else:
            normalized_slots = max(1, slots)
            value = min(1.0, max(0.0, depth / normalized_slots))
        self._temporal_worker_slot_saturation.labels(task_queue=task_queue).set(value)

    def record_rate_limit_wait(
        self,
        connector_type: str,
        wait_seconds: float,
    ) -> None:
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
        self._subprocess_executions.labels(stage=stage.value, outcome=outcome).inc()

    def stage_tracer(self) -> Any:
        return self._tracer

    def record_stage(
        self,
        stage: IngestionStage,
        outcome: str,
        duration: float,
        document_kind: str = DOCUMENT_KIND_NONE,
    ) -> None:
        self._stage_operations.labels(
            stage=stage.value, outcome=outcome, document_kind=document_kind
        ).inc()
        self._stage_duration.labels(stage=stage.value, document_kind=document_kind).observe(
            duration
        )

    def record_parser_engine_attempt(
        self,
        *,
        engine: str,
        success: bool,
        duration_seconds: float,
    ) -> None:
        outcome = "success" if success else "failure"
        self._parser_engine_attempts.labels(engine=engine, outcome=outcome).inc()
        self._parser_engine_duration.labels(engine=engine).observe(max(0.0, duration_seconds))


def build_model_telemetry(
    config: ModelClientConfig,
    *,
    langfuse_enabled: bool,
    registry: CollectorRegistry | None = None,
) -> TelemetryDispatcher:
    """Compose sanitized model telemetry for embedding or chat clients.

    Annotated against the shared client-config base so every model family --
    embed, chat -- composes its dispatcher the same way; only
    ``config.observability`` is read.

    ``OpenTelemetryTelemetry`` records into a bare OTel meter with no
    MeterProvider/exporter wired up anywhere in this codebase, so its
    histogram is otherwise discarded. Pass the caller's Prometheus
    ``registry`` (e.g. ``IngestionTelemetry.registry``) to also record
    call duration where it will actually be scraped.
    """

    sinks: list[object] = [OpenTelemetryTelemetry()]
    if registry is not None:
        sinks.append(PrometheusModelTelemetry(registry))
    if langfuse_enabled:
        sinks.append(LangfuseTelemetry())
    return TelemetryDispatcher(sinks, config=config.observability)


class PrometheusModelTelemetry:
    """Record model call counts/duration directly onto a prometheus_client registry."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self._requests = Counter(
            "harborrag_model_requests_total",
            "Model requests completed, by operation, provider, model, and status.",
            ("operation", "provider", "model", "status"),
            registry=registry,
        )
        self._duration = Histogram(
            "harborrag_model_call_duration_seconds",
            "Model call duration, by operation, provider, model, and status.",
            ("operation", "provider", "model", "status"),
            registry=registry,
        )

    def emit(self, event: TelemetryEvent) -> None:
        if event.total_duration_ms is None:
            return
        labels = {
            "operation": event.operation,
            "provider": event.provider or "unknown",
            "model": event.provider_model or event.logical_model or "unknown",
            "status": event.status.value,
        }
        self._requests.labels(**labels).inc()
        self._duration.labels(**labels).observe(max(0.0, event.total_duration_ms / 1000))

    async def aemit(self, event: TelemetryEvent) -> None:
        self.emit(event)

    def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None
