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
    """Return skip counts from the connector's latest traversal, or ``None`` if none."""
    skipped = tuple(getattr(connector, "skipped", ()) or ())
    if not skipped:
        return None
    counts = Counter(str(skip.reason) for skip in skipped)
    return {"skipped": len(skipped), "skipped_by_reason": dict(sorted(counts.items()))}


async def run_skip_summary(
    control: IngestionControlPlaneDatabase,
    task_id: str,
) -> dict[str, object]:
    """Combine discovery skips already on the task with extraction failures.

    Extraction failures are recounted from the document results on every call,
    so a retried finalization never counts them twice.
    """
    task = await control.tasks.get(task_id)
    previous = task.summary.get("skipped_by_reason") if task is not None else None
    counts: Counter[str] = Counter()
    if isinstance(previous, Mapping):
        counts.update({str(reason): int(count) for reason, count in previous.items()})
    counts.pop(EXTRACTION_FAILED, None)
    extraction_failures = await _extraction_failures(control, task_id)
    if extraction_failures:
        counts[EXTRACTION_FAILED] = extraction_failures
    return {
        "skipped": sum(counts.values()),
        "skipped_by_reason": dict(sorted(counts.items())),
    }


async def _extraction_failures(control: IngestionControlPlaneDatabase, task_id: str) -> int:
    """Count failed documents of a task whose failure happened while parsing."""
    count = 0
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
        count += sum(item.result.get("failure_stage") == _EXTRACTION_STAGE for item in page.items)
        if not page.has_more or not page.items:
            return count
        last = page.items[-1]
        after_updated_at, after_document_id = last.updated_at, last.document_id
