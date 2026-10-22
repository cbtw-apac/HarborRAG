"""Per-run skip accounting for source ingestion tasks.

A run skips an item for one of two kinds of reason. Discovery-time skips
(allowlist, size, extension, deleted, access denied, retries exhausted) are
reported by the connector through ``skipped``; extraction failures happen
later, when one document fails to parse, and are read back from the task's
document results. Both are folded into the task summary as ``skipped`` and
``skipped_by_reason``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase

EXTRACTION_FAILED = "EXTRACTION_FAILED"
DISCOVERY_SKIPS_KEY = "discovery_skipped_by_reason"
_EXTRACTION_STAGE = "ParseAndNormalize"
_RESULT_PAGE_SIZE = 200


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


async def run_skip_summary(
    control: IngestionControlPlaneDatabase,
    task_id: str,
) -> dict[str, object]:
    """Combine discovery skips already on the task with per-document skips.

    Document-level skips are recounted from the document results on every
    call and added to the discovery counts, which finalization never rewrites,
    so a retried finalization never counts them twice.
    """
    task = await control.tasks.get(task_id)
    discovered = task.summary.get(DISCOVERY_SKIPS_KEY) if task is not None else None
    counts: Counter[str] = Counter()
    if isinstance(discovered, Mapping):
        counts.update({str(reason): int(count) for reason, count in discovered.items()})
    counts.update(await _document_skips(control, task_id))
    return {
        "skipped": sum(counts.values()),
        "skipped_by_reason": dict(sorted(counts.items())),
    }


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
