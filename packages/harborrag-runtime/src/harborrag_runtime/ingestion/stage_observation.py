from __future__ import annotations

import logging
from collections.abc import Mapping
from time import perf_counter
from types import TracebackType
from typing import Any, Literal, Self

from .observability_types import IngestionStage

logger = logging.getLogger("harborrag.runtime.ingestion.observability")


class StageObservation:
    """Failure-isolated context manager for one ingestion stage execution."""

    def __init__(
        self,
        *,
        telemetry: Any,
        stage: IngestionStage,
        attempt: int,
        attributes: Mapping[str, str | int | float],
    ) -> None:
        self._telemetry = telemetry
        self._stage = stage
        self._attempt = attempt
        self._attributes = attributes
        self._started_at = 0.0
        self._span_context: Any | None = None
        self._span: Any | None = None

    def __enter__(self) -> Self:
        self._started_at = perf_counter()
        if self._attempt > 1:
            self._telemetry.record_activity_retry(self._stage)
        try:
            self._span_context = self._telemetry._tracer.start_as_current_span(
                f"harborrag.ingestion.{self._stage.value}"
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

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        outcome = "failed" if exception is not None else "succeeded"
        try:
            self._telemetry._record_stage(
                self._stage,
                outcome,
                max(0.0, perf_counter() - self._started_at),
            )
        except Exception as error:
            _log_telemetry_failure("record_metrics", error)
        if self._span is not None:
            try:
                self._span.set_attribute("harborrag.ingestion.outcome", outcome)
            except Exception as error:
                _log_telemetry_failure("set_span_outcome", error)
        if self._span_context is not None:
            try:
                self._span_context.__exit__(exception_type, exception, traceback)
            except Exception as error:
                _log_telemetry_failure("end_span", error)
        return False


def _log_telemetry_failure(operation: str, error: Exception) -> None:
    logger.warning("Telemetry %s failed (%s)", operation, type(error).__name__)
