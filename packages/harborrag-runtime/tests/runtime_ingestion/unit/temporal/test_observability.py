"""Temporal ingestion observability behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import Mock

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from temporalio.exceptions import ApplicationError

from harborrag_adapters.repositories.database.ingestion_control.queue_metrics import (
    IngestionQueueCounts,
)
from harborrag_core.ingestion import SourceAdmissionDecision
from harborrag_core.invariants import HarborInvariantError
from harborrag_core.observability.execution_context import current_execution
from harborrag_engine.ingestion.chunking.schemas import ChunkingStatistics
from harborrag_runtime.ingestion.document.models import DocumentReleaseOutcome
from harborrag_runtime.ingestion.document.stage_models import RawCaptureStageResult
from harborrag_runtime.ingestion.observability import (
    ArtifactMetricKind,
    ChunkMetricKind,
    DocumentMetricOutcome,
    IngestionStage,
    IngestionTelemetry,
)
from harborrag_runtime.ingestion.stage_observation import StageObservation
from harborrag_runtime.observability.queue_collector import LiveQueueCollector
from harborrag_runtime.temporal import activity_observability
from harborrag_runtime.temporal.activity_observability import (
    ActivityObservability,
)


class RecordingSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


class RecordingSpanContext:
    def __init__(self, span: RecordingSpan) -> None:
        self.span = span
        self.exception_type: type[BaseException] | None = None

    def __enter__(self) -> RecordingSpan:
        return self.span

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: Any,
    ) -> Literal[False]:
        self.exception_type = exception_type
        return False


class RecordingTracer:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.contexts: list[RecordingSpanContext] = []

    def start_as_current_span(self, name: str) -> RecordingSpanContext:
        self.names.append(name)
        context = RecordingSpanContext(RecordingSpan())
        self.contexts.append(context)
        return context


def test_stage_observation_records_span_duration_outcome_and_retry() -> None:
    tracer = RecordingTracer()
    telemetry = IngestionTelemetry(
        registry=CollectorRegistry(),
        tracer=tracer,
    )

    with telemetry.stage(
        IngestionStage.CHUNK,
        attempt=2,
        attributes={"harborrag.connector.type": "local"},
    ):
        pass

    output = generate_latest(telemetry.registry).decode()
    span = tracer.contexts[0].span
    assert tracer.names == ["harborrag.ingestion.chunk"]
    assert span.attributes["harborrag.ingestion.stage"] == "chunk"
    assert span.attributes["harborrag.ingestion.outcome"] == "succeeded"
    assert span.attributes["temporal.activity.attempt"] == 2
    assert 'stage="chunk"' in output
    assert 'outcome="succeeded"' in output
    assert "harborrag_ingestion_temporal_activity_retries_total" in output


def test_schedule_metrics_record_runs_latency_and_pause_state() -> None:
    telemetry = IngestionTelemetry(registry=CollectorRegistry())
    telemetry.record_scheduled_run("daily-sync", "started", schedule_to_start_seconds=2.5)
    telemetry.record_scheduled_run("daily-sync", "failed")
    telemetry.record_schedule_states({"daily-sync": (True, False, 3)})

    output = generate_latest(telemetry.registry).decode()

    assert (
        'harborrag_temporal_scheduled_runs_total{outcome="started",schedule_id="daily-sync"} 1.0'
        in output
    )
    assert (
        'harborrag_temporal_scheduled_runs_total{outcome="failed",schedule_id="daily-sync"} 1.0'
        in output
    )
    assert 'harborrag_temporal_schedule_paused{schedule_id="daily-sync"} 1.0' in output
    assert 'harborrag_temporal_schedule_expected_paused{schedule_id="daily-sync"} 0.0' in output
    assert (
        'harborrag_temporal_schedule_actions_skipped_overlap{schedule_id="daily-sync"} 3.0'
        in output
    )
    assert (
        'harborrag_temporal_schedule_to_start_seconds_count{schedule_id="daily-sync"} 1.0' in output
    )


def test_stage_observation_preserves_failures_and_marks_span() -> None:
    tracer = RecordingTracer()
    telemetry = IngestionTelemetry(
        registry=CollectorRegistry(),
        tracer=tracer,
    )

    with pytest.raises(RuntimeError, match="source failure"):
        with telemetry.stage(IngestionStage.VERIFICATION):
            raise RuntimeError("source failure")

    output = generate_latest(telemetry.registry).decode()
    assert tracer.contexts[0].exception_type is RuntimeError
    assert tracer.contexts[0].span.attributes["harborrag.ingestion.outcome"] == "failed"
    assert 'outcome="failed"' in output


class _FailingSpan:
    def set_attribute(self, key: str, value: object) -> None:
        raise RuntimeError("set_attribute boom")


class _FailingSpanContext:
    def __enter__(self) -> _FailingSpan:
        return _FailingSpan()

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: Any,
    ) -> Literal[False]:
        raise RuntimeError("end_span boom")


class _FailingTracer:
    def start_as_current_span(self, _name: str) -> _FailingSpanContext:
        return _FailingSpanContext()


class _FailingTelemetry:
    """Telemetry double whose every hook raises, to prove failure isolation."""

    def __init__(self, *, fail_start_span: bool = True) -> None:
        self._fail_start_span = fail_start_span

    def record_activity_retry(self, stage: IngestionStage) -> None:
        raise RuntimeError("record_activity_retry boom")

    def record_stage(self, stage: IngestionStage, outcome: str, duration: float) -> None:
        raise RuntimeError("record_metrics boom")

    def stage_tracer(self) -> Any:
        if self._fail_start_span:
            raise RuntimeError("start_span boom")
        return _FailingTracer()


def test_stage_observation_isolates_start_span_and_retry_failures() -> None:
    observation = StageObservation(
        telemetry=cast(Any, _FailingTelemetry(fail_start_span=True)),
        stage=IngestionStage.CHUNK,
        attempt=2,
        attributes={},
    )

    entered = observation.__enter__()
    assert entered is observation

    assert observation.__exit__(None, None, None) is False


def test_stage_observation_isolates_span_and_metric_failures() -> None:
    observation = StageObservation(
        telemetry=cast(Any, _FailingTelemetry(fail_start_span=False)),
        stage=IngestionStage.CHUNK,
        attempt=1,
        attributes={},
    )

    observation.__enter__()

    assert observation.__exit__(None, None, None) is False


def test_domain_counters_use_bounded_labels_and_never_record_zeroes() -> None:
    telemetry = IngestionTelemetry(registry=CollectorRegistry())

    telemetry.record_documents(DocumentMetricOutcome.DISCOVERED, "local", 2)
    telemetry.record_documents(DocumentMetricOutcome.FAILED, "custom-provider")
    telemetry.record_documents(DocumentMetricOutcome.ADMITTED, "github_repository")
    telemetry.record_artifact_bytes(ArtifactMetricKind.RAW, 512)
    telemetry.record_artifact_bytes(ArtifactMetricKind.CANONICAL, 0)
    telemetry.record_chunks(ChunkMetricKind.ROUTE, 1)
    telemetry.record_chunk_tokens(64)
    telemetry.record_rate_limit_wait("jira", 0.25)
    telemetry.record_discovery_page(
        "custom-provider",
        root_count=25,
        duration_seconds=1.5,
        replayed=False,
    )
    telemetry.record_discovery_page(
        "custom-provider",
        root_count=25,
        duration_seconds=0,
        replayed=True,
    )
    telemetry.record_temporal_worker_slots("harborrag-discovery", 6)
    telemetry.record_temporal_queue_depth("harborrag-discovery", 12)
    telemetry.record_backlog_capacity_ratio(
        "harborrag-discovery",
        slots=6,
        depth=12,
    )
    telemetry.record_cleanup_backlog(3)
    telemetry.record_stale_candidate_rejections(2)

    output = generate_latest(telemetry.registry).decode()
    assert 'connector_type="local",outcome="discovered"' in output
    assert 'connector_type="other",outcome="failed"' in output
    assert 'connector_type="github",outcome="admitted"' in output
    assert 'kind="raw"' in output
    assert 'kind="canonical"' not in output
    assert "harborrag_ingestion_chunk_tokens_total 64.0" in output
    assert 'connector_type="jira"' in output
    assert 'connector_type="other",outcome="fetched"' in output
    assert 'connector_type="other",outcome="replayed"' in output
    assert "harborrag_ingestion_discovery_page_duration_seconds_sum" in output
    assert 'harborrag_temporal_task_queue_depth{task_queue="harborrag-discovery"} 12.0' in output
    assert 'harborrag_temporal_worker_slots{task_queue="harborrag-discovery"} 6.0' in output
    assert (
        'harborrag_ingestion_backlog_capacity_ratio{task_queue="harborrag-discovery"} 2.0' in output
    )
    assert "harborrag_ingestion_cleanup_backlog 3.0" in output
    assert "harborrag_retrieval_stale_candidates_rejected_total 2.0" in output


def test_unchanged_document_records_one_terminal_skip_outcome() -> None:
    telemetry = Mock(spec=IngestionTelemetry)
    observability = ActivityObservability(telemetry)
    capture = RawCaptureStageResult(
        document_id="document:stable",
        document_version_id="document-version:active",
        decision=SourceAdmissionDecision.UNCHANGED,
    )

    observability.record_capture(capture, "local")
    observability.record_publication(
        DocumentReleaseOutcome(
            document_id=capture.document_id,
            document_version_id=capture.document_version_id,
            decision=capture.decision,
        ),
        "local",
    )

    telemetry.record_documents.assert_called_once_with(
        DocumentMetricOutcome.SKIPPED,
        "local",
    )


def test_activity_observability_translates_successful_stage_results() -> None:
    telemetry = Mock(spec=IngestionTelemetry)
    observability = ActivityObservability(telemetry)
    raw_reference = SimpleNamespace(source_artifact=SimpleNamespace(byte_size=512))
    capture = SimpleNamespace(unchanged=False, raw_reference=raw_reference)
    prepared = SimpleNamespace(canonical_reference=SimpleNamespace(byte_size=256))
    statistics = ChunkingStatistics(
        route_chunk_count=2,
        evidence_chunk_count=3,
        table_chunk_count=1,
        rejected_chunk_count=1,
        total_token_count=42,
    )

    observability.record_discovery("jira", 3, replayed=False)
    observability.record_discovery("jira", 2, replayed=True)
    observability.record_discovery_page(
        "jira",
        root_count=3,
        duration_seconds=0.25,
        replayed=False,
    )
    observability.record_capture(cast(Any, capture), "jira")
    observability.record_prepared(cast(Any, prepared))
    observability.record_prepared(cast(Any, SimpleNamespace(canonical_reference=None)))
    observability.record_chunking(None)
    observability.record_chunking(statistics)
    observability.record_publication(cast(Any, SimpleNamespace(published=True)), "jira")
    observability.record_document_failure("jira")

    telemetry.record_documents.assert_any_call(DocumentMetricOutcome.DISCOVERED, "jira", 3)
    telemetry.record_documents.assert_any_call(DocumentMetricOutcome.REPLAYED, "jira", 2)
    telemetry.record_documents.assert_any_call(DocumentMetricOutcome.ADMITTED, "jira")
    telemetry.record_documents.assert_any_call(DocumentMetricOutcome.ACTIVATED, "jira")
    telemetry.record_documents.assert_any_call(DocumentMetricOutcome.FAILED, "jira")
    telemetry.record_artifact_bytes.assert_any_call(ArtifactMetricKind.RAW, 512)
    telemetry.record_artifact_bytes.assert_any_call(ArtifactMetricKind.CANONICAL, 256)
    telemetry.record_chunk_tokens.assert_called_once_with(42)


def test_capture_without_a_raw_reference_fails_the_stage_invariant() -> None:
    observability = ActivityObservability(Mock(spec=IngestionTelemetry))
    capture = SimpleNamespace(unchanged=False, raw_reference=None)

    with pytest.raises(HarborInvariantError, match="raw_reference must not be None"):
        observability.record_capture(cast(Any, capture), "jira")


def test_application_error_is_preserved_and_verification_failure_is_counted() -> None:
    telemetry = IngestionTelemetry(registry=CollectorRegistry())
    observability = ActivityObservability(telemetry)
    error = ApplicationError("safe failure", type="verification_failed", non_retryable=True)

    with pytest.raises(ApplicationError) as raised:
        with observability.boundary("VerifyProjections"):
            raise error

    assert raised.value is error
    output = generate_latest(telemetry.registry).decode()
    assert "harborrag_ingestion_projection_verification_failures_total 1.0" in output


def test_boundary_tags_span_and_binds_temporal_identity(monkeypatch) -> None:
    tracer = RecordingTracer()
    telemetry = IngestionTelemetry(registry=CollectorRegistry(), tracer=tracer)
    info = SimpleNamespace(
        namespace="harborrag",
        workflow_id="harborrag-document:task-1:0:3",
        workflow_run_id="run-1",
        activity_id="7",
        attempt=2,
    )
    monkeypatch.setattr(activity_observability.activity, "info", lambda: info)

    with ActivityObservability(telemetry).boundary("ChunkAndValidate"):
        bound = current_execution()

    expected = {
        "temporal.namespace": "harborrag",
        "temporal.workflow_id": "harborrag-document:task-1:0:3",
        "temporal.run_id": "run-1",
        "temporal.activity_id": "7",
        "temporal.attempt": 2,
    }
    attributes = tracer.contexts[0].span.attributes
    assert {key: attributes.get(key) for key in expected} == expected
    assert attributes["harborrag.document_execution_id"] == info.workflow_id
    assert bound is not None and bound.run_id == "run-1"
    assert current_execution() is None


def _counts(**overrides: Any) -> IngestionQueueCounts:
    values: dict[str, Any] = {
        "pending_by_connector": {"jira": 4, "custom-provider": 1},
        "oldest_pending_at": datetime.fromtimestamp(900.0, UTC),
        "in_progress_by_state": {"RAW_CAPTURED": 2},
        "enrichment_pending": 3,
    }
    return IngestionQueueCounts(**(values | overrides))


def test_queue_collector_reports_live_counts_and_seeds_zeroes() -> None:
    clock = Mock(return_value=1000.0)
    collector = LiveQueueCollector(15.0, clock=clock)
    registry = CollectorRegistry()
    registry.register(collector)

    collector.record(_counts())
    output = generate_latest(registry).decode()

    assert 'harborrag_ingestion_documents_pending{connector_type="jira"} 4.0' in output
    assert 'harborrag_ingestion_documents_pending{connector_type="other"} 1.0' in output
    assert 'harborrag_ingestion_documents_pending{connector_type="confluence"} 0.0' in output
    assert "harborrag_ingestion_pending_oldest_age_seconds 100.0" in output
    assert 'harborrag_ingestion_documents_in_progress{stage="raw_captured"} 2.0' in output
    assert 'harborrag_ingestion_documents_in_progress{stage="verified"} 0.0' in output
    assert "harborrag_ingestion_enrichment_pending 3.0" in output
    assert "harborrag_ingestion_queue_collected_timestamp_seconds 1000.0" in output


def test_queue_collector_reports_nothing_before_or_two_intervals_after_collection() -> None:
    clock = Mock(return_value=1000.0)
    collector = LiveQueueCollector(15.0, clock=clock)
    registry = CollectorRegistry()
    registry.register(collector)
    assert "harborrag_ingestion_documents_pending{" not in generate_latest(registry).decode()

    collector.record(_counts())
    clock.return_value = 1030.0
    assert "harborrag_ingestion_enrichment_pending 3.0" in generate_latest(registry).decode()

    clock.return_value = 1030.1
    output = generate_latest(registry).decode()
    assert "harborrag_ingestion_documents_pending{" not in output
    assert "harborrag_ingestion_enrichment_pending " not in output
    assert "harborrag_ingestion_queue_collected_timestamp_seconds " not in output


def test_enrichment_lag_is_recorded_by_enrichment_kind() -> None:
    telemetry = IngestionTelemetry(registry=CollectorRegistry())

    telemetry.record_enrichment_lag("summary", 42.0)
    telemetry.record_enrichment_lag("summary", -1.0)

    output = generate_latest(telemetry.registry).decode()
    assert 'harborrag_ingestion_enrichment_lag_seconds_count{enrichment="summary"} 1.0' in output
    assert 'harborrag_ingestion_enrichment_lag_seconds_sum{enrichment="summary"} 42.0' in output


def test_schedule_states_seed_outcome_series_so_the_first_failure_alerts() -> None:
    telemetry = IngestionTelemetry(registry=CollectorRegistry())
    telemetry.record_schedule_states({"daily-sync": (False, False, 0)})

    output = generate_latest(telemetry.registry).decode()

    assert (
        'harborrag_temporal_scheduled_runs_total{outcome="failed",schedule_id="daily-sync"} 0.0'
        in output
    )


def test_first_recorded_run_seeds_every_outcome_series() -> None:
    telemetry = IngestionTelemetry(registry=CollectorRegistry())
    telemetry.record_scheduled_run("daily-sync", "started")

    output = generate_latest(telemetry.registry).decode()

    assert (
        'harborrag_temporal_scheduled_runs_total{outcome="failed",schedule_id="daily-sync"} 0.0'
        in output
    )
