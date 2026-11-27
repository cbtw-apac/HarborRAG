"""Chat turn metrics: turns by outcome, turn time, time to the answer, and citation checks.

Labels are the chat mode, an outcome and a citation result only. No query, prompt or
answer text is recorded. Instruments come from the global OpenTelemetry meter, so they
record nothing until export is configured.

Typical usage example:

    turn = telemetry.start_turn("instant", tenant_id="tenant-a")
    turn.answered()
    turn.finish(ChatTurnStatus.OK)
"""

from __future__ import annotations

from enum import StrEnum
from functools import cache
from time import perf_counter

from opentelemetry import metrics

_DURATION_BUCKETS_SECONDS = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0, 120.0)


class ChatTurnStatus(StrEnum):
    """How one chat turn ended; only ``ERROR`` is a server-side failure."""

    OK = "ok"
    REFUSED = "refused"
    CANCELLED = "cancelled"
    ERROR = "error"


class CitationCheck(StrEnum):
    """Whether an answer cites the passages it was given.

    ``PARTIAL`` means it cites some passages but also has markers that name none.
    """

    PASSED = "pass"
    PARTIAL = "partial"
    FAILED = "fail"


class ChatTurnObservation:
    """The timing of one chat turn, recorded once when it finishes.

    Args:
        telemetry: Records the turn when it finishes.
        mode: The chat mode, such as ``instant`` or ``classic``.
        tenant_id: The tenant whose corpus the turn reads.
    """

    def __init__(self, telemetry: ChatTelemetry, mode: str, tenant_id: str) -> None:
        self._telemetry = telemetry
        self._mode = mode
        self._tenant_id = tenant_id
        self._started = perf_counter()
        self._answered_after: float | None = None
        self._finished = False

    def answered(self) -> None:
        """Mark the moment the first answer text is ready, once."""
        if self._answered_after is None:
            self._answered_after = perf_counter() - self._started

    def finish(self, status: ChatTurnStatus) -> None:
        """Record the turn's outcome and timings. Later calls change nothing."""
        if self._finished:
            return
        self._finished = True
        self._telemetry.record_turn(
            self._mode,
            self._tenant_id,
            status,
            duration_seconds=perf_counter() - self._started,
            answered_after_seconds=self._answered_after,
        )


class ChatTelemetry:
    """Record chat turns and the citation checks of their answers.

    Args:
        meter: Meter for the instruments. The global ``harborrag.chat`` meter when omitted.
    """

    def __init__(self, *, meter: metrics.Meter | None = None) -> None:
        meter = meter or metrics.get_meter("harborrag.chat")
        self._turns = meter.create_counter(
            "harborrag.chat.turns", unit="1", description="Chat turns by mode and outcome."
        )
        self._duration = meter.create_histogram(
            "harborrag.chat.turn.duration",
            unit="s",
            description="Time from the start of a chat turn to its end.",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS_SECONDS,
        )
        self._ttft = meter.create_histogram(
            "harborrag.chat.ttft",
            unit="s",
            description="Time from the start of a chat turn to its first answer text.",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS_SECONDS,
        )
        self._citations = meter.create_counter(
            "harborrag.chat.citation.checks", unit="1", description="Answer citation checks."
        )

    def start_turn(self, mode: str, *, tenant_id: str) -> ChatTurnObservation:
        """Start timing one chat turn.

        Args:
            mode: The chat mode, such as ``instant`` or ``classic``.
            tenant_id: The tenant whose corpus the turn reads.

        Returns:
            The observation to mark when the answer is ready and when the turn ends.
        """
        return ChatTurnObservation(self, mode, tenant_id)

    def record_turn(
        self,
        mode: str,
        tenant_id: str,
        status: ChatTurnStatus,
        *,
        duration_seconds: float,
        answered_after_seconds: float | None,
    ) -> None:
        """Count one finished turn and record its timings.

        Args:
            mode: The chat mode.
            tenant_id: The tenant whose corpus the turn read.
            status: How the turn ended.
            duration_seconds: Time the whole turn took.
            answered_after_seconds: Time to the first answer text. None when no
                answer was produced.
        """
        labels = {"mode": mode, "tenant": tenant_id}
        self._turns.add(1, {**labels, "status": status.value})
        self._duration.record(duration_seconds, labels)
        if answered_after_seconds is not None:
            self._ttft.record(answered_after_seconds, labels)

    def record_citation_check(self, check: CitationCheck) -> None:
        """Count one answer's citation check.

        Args:
            check: Whether the answer cites the passages it was given.
        """
        self._citations.add(1, {"result": check.value})


@cache
def shared_chat_telemetry() -> ChatTelemetry:
    """Return the process's chat telemetry, created on first use."""
    return ChatTelemetry()
