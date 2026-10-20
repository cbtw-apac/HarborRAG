"""Live ingestion queue gauges that go missing, never read 0, when collection stalls."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from harborrag_adapters.connectors import connector_registry
from harborrag_adapters.repositories.database.ingestion_control.queue_metrics import (
    IN_PROGRESS_STATES,
    IngestionQueueCounts,
)

from .connector_metrics import connector_metric_label


@dataclass(frozen=True, slots=True)
class _QueueSnapshot:
    pending_by_connector: dict[str, int]
    oldest_pending_age_seconds: float
    in_progress_by_stage: dict[str, int]
    enrichment_pending: int
    collected_at: float


class LiveQueueCollector(Collector):
    def __init__(
        self,
        interval_seconds: float,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._snapshot: _QueueSnapshot | None = None

    def record(self, counts: IngestionQueueCounts) -> None:
        now = self._clock()
        # Seed every known label at 0 so an empty queue reads 0 instead of missing.
        pending = dict.fromkeys(
            (*map(connector_metric_label, connector_registry.canonical_names()), "other"), 0
        )
        for connector_type, count in counts.pending_by_connector.items():
            label = connector_metric_label(connector_type)
            pending[label] += count
        in_progress = {state.value.lower(): 0 for state in IN_PROGRESS_STATES}
        for state, count in counts.in_progress_by_state.items():
            in_progress[state.lower()] = count
        oldest = counts.oldest_pending_at
        self._snapshot = _QueueSnapshot(
            pending_by_connector=pending,
            oldest_pending_age_seconds=(
                max(0.0, now - oldest.timestamp()) if oldest is not None else 0.0
            ),
            in_progress_by_stage=in_progress,
            enrichment_pending=counts.enrichment_pending,
            collected_at=now,
        )

    def describe(self) -> Iterable[GaugeMetricFamily]:
        return _families(None)

    def collect(self) -> Iterable[GaugeMetricFamily]:
        snapshot = self._snapshot
        if snapshot is None or self._clock() - snapshot.collected_at > 2 * self._interval_seconds:
            return ()
        return _families(snapshot)


def _families(snapshot: _QueueSnapshot | None) -> tuple[GaugeMetricFamily, ...]:
    pending = GaugeMetricFamily(
        "harborrag_ingestion_documents_pending",
        "Documents admitted by running ingestion tasks that have no outcome yet, by connector.",
        labels=("connector_type",),
    )
    oldest = GaugeMetricFamily(
        "harborrag_ingestion_pending_oldest_age_seconds",
        "Time since the oldest running task with pending documents started.",
    )
    in_progress = GaugeMetricFamily(
        "harborrag_ingestion_documents_in_progress",
        "Document versions between capture and publication, by pipeline stage.",
        labels=("stage",),
    )
    enrichment = GaugeMetricFamily(
        "harborrag_ingestion_enrichment_pending",
        "Summary scopes waiting for or running enrichment.",
    )
    collected = GaugeMetricFamily(
        "harborrag_ingestion_queue_collected_timestamp_seconds",
        "Unix time of the last successful queue collection.",
    )
    if snapshot is not None:
        for connector_type, count in snapshot.pending_by_connector.items():
            pending.add_metric((connector_type,), count)
        oldest.add_metric((), snapshot.oldest_pending_age_seconds)
        for stage, count in snapshot.in_progress_by_stage.items():
            in_progress.add_metric((stage,), count)
        enrichment.add_metric((), snapshot.enrichment_pending)
        collected.add_metric((), snapshot.collected_at)
    return pending, oldest, in_progress, enrichment, collected
