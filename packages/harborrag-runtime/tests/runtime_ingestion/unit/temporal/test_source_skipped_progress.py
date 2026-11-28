"""Discovery-time skips reach the workflow progress query and survive continuation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_runtime.ingestion.source.service import SourceIngestionService
from harborrag_runtime.temporal.schemas import SourceContinuation, SourceDiscoveryResult
from harborrag_runtime.temporal.source_workflow import SourceIngestionWorkflow


def test_get_progress_reports_discovery_skips_as_skipped() -> None:
    workflow = SourceIngestionWorkflow()
    workflow._skipped_by_reason = {"unsupported_extension": 2, "size_limit_exceeded": 1}

    progress = workflow.get_progress()

    assert progress["skipped"] == 3


def test_get_progress_reports_zero_skipped_without_discovery_skips() -> None:
    workflow = SourceIngestionWorkflow()

    assert workflow.get_progress()["skipped"] == 0


def test_discovery_result_defaults_to_no_skips() -> None:
    """Payloads recorded before the field existed still decode with no skips."""

    result = SourceDiscoveryResult(
        scan_id="scan-1",
        plan_reference=None,  # type: ignore[arg-type]
        document_count=3,
    )

    assert result.skipped_by_reason == {}


def test_continuation_carries_skips_across_workflow_histories() -> None:
    continuation = SourceContinuation(
        scan_id="scan-1",
        plan_reference=None,  # type: ignore[arg-type]
        document_count=3,
        next_document_index=0,
        batch_number=0,
        summary=None,  # type: ignore[arg-type]
        skipped_by_reason={"unsupported_extension": 2},
    )

    assert continuation.skipped_by_reason == {"unsupported_extension": 2}


@pytest.mark.asyncio
async def test_discovery_skip_counts_read_the_task_summary() -> None:
    task = SimpleNamespace(
        summary={
            "discovered": 3,
            "discovery_skipped_by_reason": {"unsupported_extension": 2, "bad": True},
        }
    )

    class _Tasks:
        async def get(self, task_id: str) -> object:
            assert task_id == "task-1"
            return task

    service = SourceIngestionService.__new__(SourceIngestionService)
    service._control = SimpleNamespace(tasks=_Tasks())  # type: ignore[attr-defined]

    counts = await service.discovery_skip_counts("task-1")

    assert counts == {"unsupported_extension": 2}


@pytest.mark.asyncio
async def test_discovery_skip_counts_empty_when_task_is_missing() -> None:
    class _Tasks:
        async def get(self, task_id: str) -> None:
            return None

    service = SourceIngestionService.__new__(SourceIngestionService)
    service._control = SimpleNamespace(tasks=_Tasks())  # type: ignore[attr-defined]

    assert await service.discovery_skip_counts("missing") == {}


def test_ingestion_result_reports_skips_and_defaults_to_none() -> None:
    from harborrag_runtime.temporal.schemas import SourceIngestionResult

    reported = SourceIngestionResult(
        task_id="task-1",
        scan_id="scan-1",
        discovered=4,
        published=4,
        unchanged=0,
        failed=0,
        removal_candidates=(),
        unresolved_relations=0,
        skipped=2,
        skipped_by_reason={"unsupported_extension": 2},
    )
    legacy = SourceIngestionResult(
        task_id="task-1",
        scan_id="scan-1",
        discovered=4,
        published=4,
        unchanged=0,
        failed=0,
        removal_candidates=(),
        unresolved_relations=0,
    )

    assert reported.skipped == 2
    assert reported.skipped_by_reason == {"unsupported_extension": 2}
    assert legacy.skipped == 0
    assert legacy.skipped_by_reason == {}
