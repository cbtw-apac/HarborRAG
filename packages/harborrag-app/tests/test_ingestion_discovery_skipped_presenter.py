"""Discovery-time skips are reported in the task's ``skipped`` progress count."""

from __future__ import annotations

import pytest

from harborrag_app.workflow_control.control_plane.connection_views import ingestion_counters
from harborrag_app.workflow_control.ingestion.presenters import (
    discovery_skipped_count,
    task_response,
)
from harborrag_core.ingestion import IngestionTask, IngestionTaskState

pytestmark = pytest.mark.unit


def _task(summary: dict[str, object]) -> IngestionTask:
    return IngestionTask(
        task_id="task-1",
        source_scope_id="scope-1",
        status=IngestionTaskState.COMPLETED,
        request={
            "tenant_id": "tenant-1",
            "connector_type": "local",
            "connection_id": "conn-1",
        },
        summary=summary,
    )


def test_discovery_skipped_count_sums_discovery_reasons_only() -> None:
    summary = {
        "skipped": 5,
        "discovery_skipped_by_reason": {"unsupported_extension": 2, "size_limit_exceeded": 1},
    }

    assert discovery_skipped_count(summary) == 3


def test_discovery_skipped_count_is_zero_without_discovery_skips() -> None:
    assert discovery_skipped_count({}) == 0


def test_task_response_includes_discovery_skips_in_progress_skipped() -> None:
    task = _task({"discovery_skipped_by_reason": {"unsupported_extension": 2}})

    response = task_response(task, {"published": 4, "unchanged": 1})

    assert response["progress"]["skipped"] == 3


def test_ingestion_counters_includes_discovery_skips() -> None:
    task = _task({"discovered": 3, "discovery_skipped_by_reason": {"unsupported_extension": 2}})

    counters = ingestion_counters(task, {"published": 3})

    assert counters == {"discovered": 5, "processed": 3, "skipped": 2, "failed": 0}


def test_ingestion_counters_mid_run_leave_pending_documents_outstanding() -> None:
    """``stage.progress`` counts processed, skipped and failed against ``discovered``.

    With 6 documents planned and 4 files skipped at discovery, 2 published
    documents must not read as a finished run.
    """
    task = _task({"discovered": 6, "discovery_skipped_by_reason": {"unsupported_extension": 4}})

    counters = ingestion_counters(task, {"published": 2})

    assert counters == {"discovered": 10, "processed": 2, "skipped": 4, "failed": 0}
