"""Time, count and trace one ingestion stage execution, isolated from telemetry failures.

A broken exporter or meter must never fail the stage it observes, so every telemetry
call here is guarded and logged instead. A failed stage marks its span with the cause's
type and code through ``record_failure``, never with the exception's text.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import AbstractContextManager
from time import perf_counter
from types import TracebackType
from typing import Literal, Protocol, Self

from opentelemetry import trace

from harborrag_core.observability.log_events import FailureKind, log_failure

from .observability_types import IngestionStage
from .span_failures import record_failure

logger = logging.getLogger("harborrag.runtime.ingestion.observability")


class _StageTelemetry(Protocol):
    """Telemetry surface required by StageObservation."""

    def record_activity_retry(self, stage: IngestionStage) -> None: ...

    def record_stage(self, stage: IngestionStage, outcome: str, duration: float) -> None: ...

    def stage_tracer(self) -> trace.Tracer: ...


class StageObservation:
    """Failure-isolated context manager for one ingestion stage execution.

    Args:
        telemetry: Where the stage's duration, outcome and retries are recorded.
        stage: The stage being run.
        attempt: The activity attempt, 1 for the first; later attempts count a retry.
        attributes: Extra span attributes, such as the workflow and activity IDs.
    """

    def __init__(
        self,
        *,
        telemetry: _StageTelemetry,
        stage: IngestionStage,
        attempt: int,
        attributes: Mapping[str, str | int | float],
    ) -> None:
        self._telemetry = telemetry
        self._stage = stage
        self._attempt = attempt
        self._attributes = attributes
        self._started_at = 0.0
        self._span_context: AbstractContextManager[trace.Span] | None = None
        self._span: trace.Span | None = None
        self._failure_recorded = False

    def __enter__(self) -> Self:
        """Start timing and open the ``harborrag.ingestion.<stage>`` span."""
        self._started_at = perf_counter()
        if self._attempt > 1:
            try:
                self._telemetry.record_activity_retry(self._stage)
            except Exception as error:
                _log_telemetry_failure("record_activity_retry", error)
        try:
            self._span_context = self._telemetry.stage_tracer().start_as_current_span(
                f"harborrag.ingestion.{self._stage.value}",
                record_exception=False,
                set_status_on_exception=False,
            )
            self._span = self._span_context.__enter__()
            self._span.set_attribute("harborrag.ingestion.stage", self._stage.value)
            self._span.set_attribute("temporal.activity.attempt", self._attempt)
            for key, value in self._attributes.items():
                self._span.set_attribute(key, value)
        except Exception as error:
            self._span_context = None
            self._span = None
            _log_telemetry_failure("start_span", error)
        return self

    def fail(self, error: BaseException, *, code: str, retryable: bool) -> None:
        """Record the stage's classified failure on its span before the error is wrapped.

        Use it where the error is about to be replaced, such as by a Temporal failure
        raised ``from None``: the span then names the original cause and its code.

        Args:
            error: The original error.
            code: The failure's safe code.
            retryable: Whether the failure will be retried.
        """
        if self._span is None:
            return
        try:
            record_failure(self._span, error, code=code, retryable=retryable)
            self._failure_recorded = True
        except Exception as telemetry_error:  # noqa: BLE001 - telemetry must never fail the stage
            _log_telemetry_failure("record_failure", telemetry_error)

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Record the duration and outcome, mark a failure on the span, and end it."""
        outcome = "failed" if exception is not None else "succeeded"
        try:
            self._telemetry.record_stage(
                self._stage,
                outcome,
                max(0.0, perf_counter() - self._started_at),
            )
        except Exception as error:
            _log_telemetry_failure("record_metrics", error)
        if self._span is not None:
            try:
                self._span.set_attribute("harborrag.ingestion.outcome", outcome)
                if exception is not None and not self._failure_recorded:
                    record_failure(self._span, exception)
            except Exception as error:
                _log_telemetry_failure("set_span_outcome", error)
        if self._span_context is not None:
            try:
                self._span_context.__exit__(exception_type, exception, traceback)
            except Exception as error:
                _log_telemetry_failure("end_span", error)
        return False


def _log_telemetry_failure(operation: str, error: Exception) -> None:
    """Log a telemetry call that failed; the stage it observed carries on."""
    log_failure(
        logger, "telemetry.operation_failed", error, FailureKind.EXPECTED, operation=operation
    )
