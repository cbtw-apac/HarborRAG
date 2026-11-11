"""Metrics and Temporal failure translation at ingestion activity boundaries.

Entry point: ActivityObservability.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import perf_counter

from temporalio import activity
from temporalio.exceptions import ApplicationError

from harborrag_core.ingestion import IngestionFailureClassifier
from harborrag_core.invariants import HarborInvariantError
from harborrag_engine.ingestion.chunking.schemas import ChunkingStatistics
from harborrag_engine.ingestion.release.stage_models import (
    PreparedDocumentStage,
    RawCaptureStageResult,
)
from harborrag_runtime.ingestion.document.models import DocumentReleaseOutcome
from harborrag_runtime.ingestion.failure_classification import ingestion_failure_classifier
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
    "PauseSourceIngestion": IngestionStage.CONTROL,
    "ResumeSourceIngestion": IngestionStage.CONTROL,
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
    """Decorate activity boundaries and translate stage results into metrics.

    Args:
        telemetry: Ingestion metrics sink.
        failures: Classifier of stage failures; the ingestion default when omitted.
    """

    def __init__(
        self,
        telemetry: IngestionTelemetry,
        *,
        failures: IngestionFailureClassifier | None = None,
    ) -> None:
        self._telemetry = telemetry
        self._failures = failures or ingestion_failure_classifier()

    @contextmanager
    def boundary(self, stage: str) -> Iterator[None]:
        """Emit telemetry and convert domain failures into Temporal retry intent.

        A connector that declines one item names the standard skip reason; it rides in the
        failure details so the document result can record it.

        Args:
            stage: Activity stage name; must be in ``_ACTIVITY_STAGES``.

        Raises:
            HarborInvariantError: The stage has no metric stage.
            ApplicationError: The wrapped activity failed; retryable per its classification.
        """
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
                    "attempt=%d error_code=%s error_type=%s status_code=%s retryable=%s",
                    stage,
                    context.workflow_id,
                    context.activity_id,
                    context.attempt,
                    failure.code,
                    type(error).__name__,
                    getattr(error, "status_code", None),
                    failure.retryable,
                )
                skip_reason = getattr(error, "skip_reason", None)
                details = (skip_reason,) if isinstance(skip_reason, str) and skip_reason else ()
                raise ApplicationError(
                    "ingestion activity failed; inspect restricted worker logs",
                    *details,
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
        """Count discovered or replayed documents of a connector."""
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
        """Record one discovery page's size and duration."""
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
        """Count an admitted capture and its raw artifact bytes.

        An unchanged capture records nothing: publication is the terminal document outcome,
        including the idempotent no-op for unchanged versions, so counting the skip there
        keeps one document from incrementing the outcome twice.

        Raises:
            HarborInvariantError: A changed capture has no raw reference.
        """
        if capture.unchanged:
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
        """Record the canonical artifact bytes of a prepared document."""
        if prepared.canonical_reference is not None:
            self._telemetry.record_artifact_bytes(
                ArtifactMetricKind.CANONICAL,
                prepared.canonical_reference.byte_size,
            )

    def record_chunking(self, statistics: ChunkingStatistics | None) -> None:
        """Record chunk counts and tokens from chunking statistics."""
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
        """Count an activated or skipped document from its release outcome."""
        self._telemetry.record_documents(
            (
                DocumentMetricOutcome.ACTIVATED
                if outcome.published
                else DocumentMetricOutcome.SKIPPED
            ),
            connector_type,
        )

    def record_document_failure(self, connector_type: str) -> None:
        """Count a failed document of a connector."""
        self._telemetry.record_documents(
            DocumentMetricOutcome.FAILED,
            connector_type,
        )

    def record_subprocess_outcome(self, stage: str, outcome: SubprocessOutcomeLabel) -> None:
        """Record how a stage's subprocess ended.

        Raises:
            HarborInvariantError: The stage has no metric stage.
        """
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
