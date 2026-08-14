from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import perf_counter

from temporalio import activity
from temporalio.exceptions import ApplicationError

from harborrag_core.invariants import HarborInvariantError
from harborrag_engine.ingestion import IngestionFailureClassifier
from harborrag_engine.ingestion.chunking.schemas import ChunkingStatistics
from harborrag_runtime.ingestion.document.models import DocumentReleaseOutcome
from harborrag_runtime.ingestion.document.stage_models import (
    PreparedDocumentStage,
    RawCaptureStageResult,
)
from harborrag_runtime.ingestion.observability import (
    ArtifactMetricKind,
    ChunkMetricKind,
    DocumentMetricOutcome,
    IngestionStage,
    IngestionTelemetry,
    SubprocessOutcomeLabel,
)

logger = logging.getLogger("harborrag.runtime.temporal.activities")

_ACTIVITY_STAGES = {
    "DiscoverSourceItems": IngestionStage.DISCOVERY,
    "CancelSourceIngestion": IngestionStage.CANCELLATION,
    "RecordSourceFailure": IngestionStage.FAILURE_CAPTURE,
    "FetchAndCaptureRaw": IngestionStage.FETCH,
    "ParseAndNormalize": IngestionStage.PARSE_NORMALIZE,
    "SyncContentUnits": IngestionStage.CONTENT_SYNC,
    "PersistCanonical": IngestionStage.CANONICAL_PERSIST,
    "ChunkAndValidate": IngestionStage.CHUNK,
    "EncodeChunks": IngestionStage.ENCODE,
    "BuildRelations": IngestionStage.RELATION_BUILD,
    "BuildProjections": IngestionStage.PROJECTION_BUILD,
    "WriteVectorProjection": IngestionStage.QDRANT_WRITE,
    "WriteGraphProjection": IngestionStage.FALKORDB_WRITE,
    "VerifyProjections": IngestionStage.VERIFICATION,
    "PublishVersion": IngestionStage.PUBLICATION,
    "RecordDocumentFailure": IngestionStage.FAILURE_CAPTURE,
    "FinalizeSourceIngestion": IngestionStage.FINALIZATION,
    "PrepareRetryFailures": IngestionStage.DISCOVERY,
    "RetryDocumentRelease": IngestionStage.FETCH,
    "RecordRetryDocumentFailure": IngestionStage.FAILURE_CAPTURE,
    "RecordRetryFailuresTaskFailure": IngestionStage.FAILURE_CAPTURE,
    "FinalizeRetryFailures": IngestionStage.FINALIZATION,
}


class ActivityObservability:
    """Decorate activity boundaries and translate stage results into metrics."""

    def __init__(
        self,
        telemetry: IngestionTelemetry,
        *,
        failures: IngestionFailureClassifier | None = None,
    ) -> None:
        self._telemetry = telemetry
        self._failures = failures or IngestionFailureClassifier()

    @contextmanager
    def boundary(self, stage: str) -> Iterator[None]:
        """Emit telemetry and convert domain failures into Temporal retry intent."""

        try:
            metric_stage = _ACTIVITY_STAGES[stage]
        except KeyError:
            raise HarborInvariantError(
                f"activity stage {stage!r} has no entry in _ACTIVITY_STAGES; "
                "add it before wiring this boundary() call"
            ) from None
        context = _activity_log_context()
        started_at = perf_counter()
        logger.debug(
            "Activity started stage=%s workflow_id=%s activity_id=%s attempt=%d",
            stage,
            context.workflow_id,
            context.activity_id,
            context.attempt,
        )
        with self._telemetry.stage(metric_stage, attempt=context.attempt):
            try:
                yield
            except ApplicationError as error:
                self._record_verification_failure(metric_stage)
                logger.error(
                    "Activity failed stage=%s workflow_id=%s activity_id=%s "
                    "attempt=%d error_code=%s error_type=%s",
                    stage,
                    context.workflow_id,
                    context.activity_id,
                    context.attempt,
                    error.type or "application_error",
                    type(error).__name__,
                )
                raise
            except Exception as error:
                self._record_verification_failure(metric_stage)
                failure = self._failures.classify(stage, error)
                logger.error(
                    "Activity failed stage=%s workflow_id=%s activity_id=%s "
                    "attempt=%d error_code=%s error_type=%s retryable=%s",
                    stage,
                    context.workflow_id,
                    context.activity_id,
                    context.attempt,
                    failure.code,
                    type(error).__name__,
                    failure.retryable,
                )
                raise ApplicationError(
                    "ingestion activity failed; inspect restricted worker logs",
                    type=failure.code,
                    non_retryable=not failure.retryable,
                ) from error
            else:
                logger.debug(
                    "Activity completed stage=%s workflow_id=%s activity_id=%s "
                    "attempt=%d duration_ms=%.1f",
                    stage,
                    context.workflow_id,
                    context.activity_id,
                    context.attempt,
                    (perf_counter() - started_at) * 1000,
                )

    def record_discovery(
        self,
        connector_type: str,
        count: int,
        *,
        replayed: bool,
    ) -> None:
        self._telemetry.record_documents(
            (DocumentMetricOutcome.REPLAYED if replayed else DocumentMetricOutcome.DISCOVERED),
            connector_type,
            count,
        )

    def record_discovery_page(
        self,
        connector_type: str,
        *,
        root_count: int,
        duration_seconds: float,
        replayed: bool,
    ) -> None:
        self._telemetry.record_discovery_page(
            connector_type,
            root_count=root_count,
            duration_seconds=duration_seconds,
            replayed=replayed,
        )

    def record_capture(
        self,
        capture: RawCaptureStageResult,
        connector_type: str,
    ) -> None:
        if capture.unchanged:
            # Publication is the terminal document outcome, including the
            # idempotent no-op used for unchanged versions. Count the skip
            # there so one document never increments the outcome twice.
            return
        self._telemetry.record_documents(
            DocumentMetricOutcome.ADMITTED,
            connector_type,
        )
        if capture.raw_reference is None:
            raise HarborInvariantError("capture.raw_reference must not be None here")
        self._telemetry.record_artifact_bytes(
            ArtifactMetricKind.RAW,
            capture.raw_reference.source_artifact.byte_size,
        )

    def record_prepared(self, prepared: PreparedDocumentStage) -> None:
        if prepared.canonical_reference is not None:
            self._telemetry.record_artifact_bytes(
                ArtifactMetricKind.CANONICAL,
                prepared.canonical_reference.byte_size,
            )

    def record_chunking(self, statistics: ChunkingStatistics | None) -> None:
        if statistics is None:
            return
        self._telemetry.record_chunks(
            ChunkMetricKind.ROUTE,
            statistics.route_chunk_count,
        )
        self._telemetry.record_chunks(
            ChunkMetricKind.EVIDENCE,
            statistics.evidence_chunk_count,
        )
        self._telemetry.record_chunks(
            ChunkMetricKind.TABLE,
            statistics.table_chunk_count,
        )
        self._telemetry.record_chunks(
            ChunkMetricKind.REJECTED,
            statistics.rejected_chunk_count,
        )
        self._telemetry.record_chunk_tokens(statistics.total_token_count)

    def record_publication(
        self,
        outcome: DocumentReleaseOutcome,
        connector_type: str,
    ) -> None:
        self._telemetry.record_documents(
            (
                DocumentMetricOutcome.ACTIVATED
                if outcome.published
                else DocumentMetricOutcome.SKIPPED
            ),
            connector_type,
        )

    def record_document_failure(self, connector_type: str) -> None:
        self._telemetry.record_documents(
            DocumentMetricOutcome.FAILED,
            connector_type,
        )

    def record_subprocess_outcome(self, stage: str, outcome: SubprocessOutcomeLabel) -> None:
        try:
            metric_stage = _ACTIVITY_STAGES[stage]
        except KeyError:
            raise HarborInvariantError(
                f"activity stage {stage!r} has no entry in _ACTIVITY_STAGES; "
                "add it before recording subprocess outcomes"
            ) from None
        self._telemetry.record_subprocess_outcome(metric_stage, outcome)

    def _record_verification_failure(self, stage: IngestionStage) -> None:
        if stage is IngestionStage.VERIFICATION:
            self._telemetry.record_verification_failure()


@dataclass(frozen=True, slots=True)
class _ActivityLogContext:
    workflow_id: str
    activity_id: str
    attempt: int


def _activity_log_context() -> _ActivityLogContext:
    try:
        info = activity.info()
    except RuntimeError:
        return _ActivityLogContext(
            workflow_id="outside-temporal",
            activity_id="outside-temporal",
            attempt=1,
        )
    return _ActivityLogContext(
        workflow_id=info.workflow_id or "outside-workflow",
        activity_id=info.activity_id,
        attempt=info.attempt,
    )
