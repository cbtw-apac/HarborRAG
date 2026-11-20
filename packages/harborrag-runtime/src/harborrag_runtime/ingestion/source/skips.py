"""Per-run skip accounting for source ingestion tasks.

Discovery skips come from the connector's ``skipped`` report and stay on the
task under ``DISCOVERY_SKIPS_KEY``, which status views read even after the run
ends. Per-document skips are read back from the task's document results.
Connector and database types are imported for annotations only, so importing
this module stays light.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import TYPE_CHECKING, TypedDict

from harborrag_engine.ingestion.source.models import DISCOVERY_SKIPS_KEY

if TYPE_CHECKING:
    from harborrag_adapters.connectors.base import BaseConnector
    from harborrag_adapters.connectors.harbor_connector import HarborConnector
    from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase

EXTRACTION_FAILED = "EXTRACTION_FAILED"
_EXTRACTION_STAGE = "ParseAndNormalize"
_RESULT_PAGE_SIZE = 200


class SkipSummary(TypedDict):
    """Skip counts that finalization stores on a finished task.

    Attributes:
        skipped: Items the run skipped, at discovery or per document.
        skipped_by_reason: ``skipped`` broken down by reason code.
        discovery_skipped_by_reason: The discovery part of ``skipped_by_reason``.
            Its name is ``DISCOVERY_SKIPS_KEY``, the key status views read.
    """

    skipped: int
    skipped_by_reason: dict[str, int]
    discovery_skipped_by_reason: dict[str, int]


def bind_connector_run(connector: BaseConnector | HarborConnector, run_id: str) -> None:
    """Tag a connector's structured logs with the task's run ID, when it supports it."""
    bind = getattr(connector, "bind_run_id", None)
    if callable(bind):
        bind(run_id)


def discovery_skip_summary(
    connector: BaseConnector | HarborConnector,
) -> dict[str, object] | None:
    """Return skip counts from the connector's latest traversal.

    ``None`` means the connector cannot report skips. A connector that reports
    an empty collection yields zero counts, so a retried traversal clears the
    counts an earlier one left on the task.
    """
    reported = getattr(connector, "skipped", None)
    if reported is None:
        return None
    skipped = tuple(reported)
    counts = dict(sorted(Counter(str(skip.reason) for skip in skipped).items()))
    return {
        "skipped": len(skipped),
        "skipped_by_reason": counts,
        DISCOVERY_SKIPS_KEY: counts,
    }


def carried_discovery_skips(summary: Mapping[str, object]) -> dict[str, object]:
    """Return the discovery skip counts that a replacement task summary must keep.

    Moving a task to a terminal state stores a new summary in place of the old
    one, and status views read these counts after the run ends. The result is
    empty when the summary holds no discovery counts.
    """
    stored = summary.get(DISCOVERY_SKIPS_KEY)
    return {DISCOVERY_SKIPS_KEY: dict(stored)} if isinstance(stored, Mapping) else {}


async def run_skip_summary(
    control: IngestionControlPlaneDatabase,
    task_id: str,
) -> SkipSummary:
    """Combine discovery skips already on the task with per-document skips.

    Per-document skips are recounted from the document results on every call.
    Finalization replaces the stored summary, so the discovery counts come back
    too, for it to keep on the task. A retried finalization reads them back
    unchanged and never counts a skip twice.

    Args:
        control: Control-plane database that holds the task and its results.
        task_id: Run whose skips to count.

    Returns:
        The run's skip total, its breakdown by reason, and the discovery part.
    """
    task = await control.tasks.get(task_id)
    stored = task.summary.get(DISCOVERY_SKIPS_KEY) if task is not None else None
    discovered = (
        {str(reason): int(count) for reason, count in stored.items()}
        if isinstance(stored, Mapping)
        else {}
    )
    counts: Counter[str] = Counter(discovered)
    counts.update(await _document_skips(control, task_id))
    return SkipSummary(
        skipped=sum(counts.values()),
        skipped_by_reason=dict(sorted(counts.items())),
        discovery_skipped_by_reason=dict(sorted(discovered.items())),
    )


async def _document_skips(control: IngestionControlPlaneDatabase, task_id: str) -> Counter[str]:
    """Count failed documents that are skips: item-load skips and parse failures."""
    counts: Counter[str] = Counter()
    after_updated_at = None
    after_document_id = None
    while True:
        page = await control.tasks.document_results_page(
            task_id,
            statuses=("failed",),
            after_updated_at=after_updated_at,
            after_document_id=after_document_id,
            limit=_RESULT_PAGE_SIZE,
        )
        for item in page.items:
            skip_reason = item.result.get("skip_reason")
            if isinstance(skip_reason, str) and skip_reason:
                counts[skip_reason] += 1
            elif item.result.get("failure_stage") == _EXTRACTION_STAGE:
                counts[EXTRACTION_FAILED] += 1
        if not page.has_more or not page.items:
            return counts
        last = page.items[-1]
        after_updated_at, after_document_id = last.updated_at, last.document_id
