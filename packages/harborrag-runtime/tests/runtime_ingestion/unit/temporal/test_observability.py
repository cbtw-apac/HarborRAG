"""Temporal ingestion observability behavior."""

from __future__ import annotations

from typing import Any, Literal
from unittest.mock import Mock

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from harborrag_core.ingestion import SourceAdmissionDecision
from harborrag_runtime.ingestion.document.models import DocumentReleaseOutcome
from harborrag_runtime.ingestion.document.stage_models import RawCaptureStageResult
from harborrag_runtime.ingestion.observability import (
    ArtifactMetricKind,
    ChunkMetricKind,
    DocumentMetricOutcome,
    IngestionStage,
    IngestionTelemetry,
)
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
