"""Temporal ingestion observability behavior."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import Mock

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from temporalio.exceptions import ApplicationError

from harborrag_core.ingestion import SourceAdmissionDecision
from harborrag_core.invariants import HarborInvariantError
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
    telemetry.record_temporal_worker_slot_saturation(
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
        'harborrag_temporal_worker_slot_saturation{task_queue="harborrag-discovery"} 1.0' in output
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


def test_document_duration_is_observed_once_per_terminal_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = IngestionTelemetry(registry=CollectorRegistry())
    observability = ActivityObservability(telemetry)
    monkeypatch.setattr("harborrag_runtime.temporal.activity_observability.time", lambda: 1_000.0)

    observability.record_publication(
        cast(Any, SimpleNamespace(publication=SimpleNamespace(replayed=False))),
        "jira",
        started_at=958.0,
    )
    observability.record_document_failure("jira", started_at=999.5)
    observability.record_document_failure("jira")

    def sample(name: str, outcome: str) -> float | None:
        return telemetry.registry.get_sample_value(
            f"harborrag_ingestion_document_duration_seconds_{name}",
            {"outcome": outcome, "connector_type": "jira"},
        )

    assert sample("count", "activated") == 1.0
    assert sample("sum", "activated") == 42.0
    assert sample("count", "failed") == 1.0
    assert sample("sum", "failed") == 0.5


def test_activity_observability_translates_successful_stage_results() -> None:
    telemetry = Mock(spec=IngestionTelemetry)
    observability = ActivityObservability(telemetry)
    raw_reference = SimpleNamespace(source_artifact=SimpleNamespace(byte_size=512))
    capture = SimpleNamespace(unchanged=False, raw_reference=raw_reference)
    prepared = SimpleNamespace(
        canonical_reference=SimpleNamespace(byte_size=256),
        parser_attempts=(("pymupdf", True, 12.5), ("mineru", False, 340.0)),
    )
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
    observability.record_prepared(
        cast(Any, SimpleNamespace(canonical_reference=None, parser_attempts=()))
    )
    observability.record_chunking(None)
    observability.record_chunking(statistics)
    observability.record_publication(
        cast(Any, SimpleNamespace(publication=SimpleNamespace(replayed=False))), "jira"
    )
    observability.record_document_failure("jira")

    telemetry.record_documents.assert_any_call(DocumentMetricOutcome.DISCOVERED, "jira", 3)
    telemetry.record_documents.assert_any_call(DocumentMetricOutcome.REPLAYED, "jira", 2)
    telemetry.record_documents.assert_any_call(DocumentMetricOutcome.ADMITTED, "jira")
    telemetry.record_documents.assert_any_call(DocumentMetricOutcome.ACTIVATED, "jira")
    telemetry.record_documents.assert_any_call(DocumentMetricOutcome.FAILED, "jira")
    telemetry.record_artifact_bytes.assert_any_call(ArtifactMetricKind.RAW, 512)
    telemetry.record_artifact_bytes.assert_any_call(ArtifactMetricKind.CANONICAL, 256)
    telemetry.record_chunk_tokens.assert_called_once_with(42)
    telemetry.record_parser_engine_attempt.assert_any_call(
        engine="pymupdf", success=True, duration_seconds=0.0125
    )
    telemetry.record_parser_engine_attempt.assert_any_call(
        engine="mineru", success=False, duration_seconds=0.34
    )


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


def test_subprocess_cpu_collector_reports_finished_children_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harborrag_runtime.observability import process_metrics

    fake_resource = SimpleNamespace(
        RUSAGE_CHILDREN=-1,
        getrusage=lambda who: SimpleNamespace(ru_utime=1.5, ru_stime=0.25),
    )
    monkeypatch.setattr(process_metrics, "resource", fake_resource)
    registry = CollectorRegistry()
    registry.register(process_metrics.SubprocessCpuCollector())

    assert registry.get_sample_value("harborrag_ingestion_subprocess_cpu_seconds_total") == 1.75


def test_subprocess_cpu_collector_is_silent_without_getrusage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harborrag_runtime.observability import process_metrics

    monkeypatch.setattr(process_metrics, "resource", None)
    registry = CollectorRegistry()
    registry.register(process_metrics.SubprocessCpuCollector())

    assert registry.get_sample_value("harborrag_ingestion_subprocess_cpu_seconds_total") is None


def test_owned_registry_gets_process_collectors_but_shared_registry_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harborrag_runtime.observability import process_metrics

    fake_resource = SimpleNamespace(
        RUSAGE_CHILDREN=-1,
        getrusage=lambda who: SimpleNamespace(ru_utime=2.0, ru_stime=0.0),
    )
    monkeypatch.setattr(process_metrics, "resource", fake_resource)
    sample = "harborrag_ingestion_subprocess_cpu_seconds_total"

    owned = IngestionTelemetry()
    shared = IngestionTelemetry(registry=CollectorRegistry())

    assert owned.registry.get_sample_value(sample) == 2.0
    assert shared.registry.get_sample_value(sample) is None


def test_republishing_the_active_version_is_not_counted_as_activated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telemetry = Mock(spec=IngestionTelemetry)
    observability = ActivityObservability(telemetry)
    monkeypatch.setattr("harborrag_runtime.temporal.activity_observability.time", lambda: 10.0)

    observability.record_publication(
        cast(Any, SimpleNamespace(publication=SimpleNamespace(replayed=True))),
        "local",
        started_at=4.0,
    )

    telemetry.record_documents.assert_called_once_with(
        DocumentMetricOutcome.REPUBLISHED,
        "local",
    )
    telemetry.record_document_duration.assert_called_once_with(
        DocumentMetricOutcome.REPUBLISHED,
        "local",
        6.0,
    )


def test_document_series_exist_at_zero_before_the_first_document() -> None:
    telemetry = IngestionTelemetry(registry=CollectorRegistry())

    assert (
        telemetry.registry.get_sample_value(
            "harborrag_ingestion_documents_total",
            {"outcome": "republished", "connector_type": "local"},
        )
        == 0.0
    )
    assert (
        telemetry.registry.get_sample_value(
            "harborrag_ingestion_document_duration_seconds_count",
            {"outcome": "activated", "connector_type": "other"},
        )
        == 0.0
    )


@pytest.mark.parametrize(
    ("decision", "kind"),
    [
        ("NEW", "activated"),
        ("UPDATED", "activated"),
        ("FORCE_REPROCESS", "republished"),
        ("UNCHANGED", "skipped"),
        ("UNSUPPORTED", "other"),
        (None, "none"),
    ],
)
def test_document_kind_maps_admission_decisions_to_outcome_words(
    decision: str | None, kind: str
) -> None:
    from harborrag_runtime.observability.observability_types import document_kind

    assert document_kind(decision) == kind


def test_activity_boundary_labels_stage_metrics_with_the_document_kind() -> None:
    telemetry = IngestionTelemetry(registry=CollectorRegistry())
    observability = ActivityObservability(telemetry)

    with observability.boundary("EncodeChunks", decision="FORCE_REPROCESS"):
        pass
    with observability.boundary("FetchAndCaptureRaw") as observation:
        observation.set_decision("UNCHANGED")

    def count(stage: str, kind: str) -> float | None:
        return telemetry.registry.get_sample_value(
            "harborrag_ingestion_stage_duration_seconds_count",
            {"stage": stage, "document_kind": kind},
        )

    assert count("encode", "republished") == 1.0
    assert count("encode", "activated") == 0.0
    assert count("fetch", "skipped") == 1.0


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
