from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Literal

from opentelemetry import trace
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_adapters.models.runtime import (
    LangfuseTelemetry,
    OpenTelemetryTelemetry,
    TelemetryDispatcher,
)

from .connector_metrics import connector_metric_label
from .discovery_metrics import DiscoveryMetrics
from .observability_types import (
    ArtifactMetricKind,
    ChunkMetricKind,
    DocumentMetricOutcome,
    IngestionStage,
)
from .stage_observation import StageObservation

logger = logging.getLogger("harborrag.runtime.ingestion.observability")

SubprocessOutcomeLabel = Literal[
    "success", "serialization_fail", "result_serialization_fail", "crash"
]


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
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self._tracer = tracer or trace.get_tracer("harborrag.ingestion")
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

    def record_stage(self, stage: IngestionStage, outcome: str, duration: float) -> None:
        self._stage_operations.labels(stage=stage.value, outcome=outcome).inc()
        self._stage_duration.labels(stage=stage.value).observe(duration)


def build_model_telemetry(
    config: HarborEmbedClientConfig,
    *,
    langfuse_enabled: bool,
) -> TelemetryDispatcher:
    """Compose sanitized model telemetry; Langfuse remains embedding-only here."""

    sinks: list[object] = [OpenTelemetryTelemetry()]
    if langfuse_enabled:
        sinks.append(LangfuseTelemetry())
    return TelemetryDispatcher(sinks, config=config.observability)
