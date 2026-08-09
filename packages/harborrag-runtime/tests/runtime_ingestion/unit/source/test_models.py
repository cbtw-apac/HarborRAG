"""Source ingestion value-object policy tests."""

from __future__ import annotations

import pytest

from harborrag_core.ingestion import IngestionTaskState
from harborrag_core.invariants import HarborInvariantError
from harborrag_runtime.ingestion.source.models import SourceDispatchSummary


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (SourceDispatchSummary(), IngestionTaskState.COMPLETED),
        (SourceDispatchSummary(published=2), IngestionTaskState.COMPLETED),
        (SourceDispatchSummary(failed=2), IngestionTaskState.FAILED),
        (
            SourceDispatchSummary(published=1, failed=1),
            IngestionTaskState.PARTIAL,
        ),
        (
            SourceDispatchSummary(unchanged=1, failed=1),
            IngestionTaskState.PARTIAL,
        ),
    ],
)
def test_dispatch_summary_owns_terminal_task_state_policy(
    summary: SourceDispatchSummary,
    expected: IngestionTaskState,
) -> None:
    assert summary.task_state() is expected


def test_dispatch_summary_rejects_unknown_document_outcomes() -> None:
    with pytest.raises(ValueError, match="unsupported document ingestion outcome"):
        SourceDispatchSummary.from_results(("published", "cancelled"))  # type: ignore[arg-type]


def test_dispatch_summary_rejects_negative_counts() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        SourceDispatchSummary(failed=-1)


def test_dispatch_summary_requires_complete_plan_accounting() -> None:
    summary = SourceDispatchSummary(published=1, failed=1)

    summary.require_total(2)
    with pytest.raises(HarborInvariantError, match="does not match"):
        summary.require_total(3)
