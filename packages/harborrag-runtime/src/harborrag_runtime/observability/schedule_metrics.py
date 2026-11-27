"""The ``/v1/admin/schedules`` slot counter of the API process, for alerts on skipped slots.

Labels stay low-cardinality: never a schedule ID or a tenant.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from prometheus_client import Counter
from prometheus_client.metrics_core import Metric

from harborrag_core.domain.schedule import ScheduleSlot

NO_REASON: Final = "none"


class ScheduleSlotMetrics:
    """Count recorded schedule slots by outcome and by why a slot did not start.

    The counter belongs to no registry; the process that serves metrics registers
    this object on its own.
    """

    def __init__(self) -> None:
        self._slots = Counter(
            "harborrag_schedule_slots_total",
            "Schedule slots recorded, by outcome and the reason a slot was skipped or queued.",
            ("outcome", "reason"),
            registry=None,
        )

    def record(self, slot: ScheduleSlot) -> None:
        """Count one recorded slot; a slot without a reason counts as ``none``."""
        self._slots.labels(outcome=slot.outcome, reason=slot.reason or NO_REASON).inc()

    def collect(self) -> Iterable[Metric]:
        """Return the counter's samples, for a Prometheus registry."""
        return self._slots.collect()


SCHEDULE_SLOT_METRICS: Final = ScheduleSlotMetrics()
"""The process's slot counter: the schedule use cases count into it, the API exposes it."""
