"""Source ingestion value-object policy tests."""

from __future__ import annotations

import pytest

from harborrag_core.ingestion import IngestionTaskState
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
