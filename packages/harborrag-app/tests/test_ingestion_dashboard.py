from __future__ import annotations

import logging

import pytest
from app_test_fixtures import MockAppService
from textual.widgets import DataTable, ProgressBar

from harborrag_app.cli.dashboard import DashboardSnapshot, IngestionDashboard
from harborrag_app.workflow_control import AppResponse


class DashboardService(MockAppService):
    def __init__(self) -> None:
        self.controls: list[str] = []

    async def ingestion_status(self, run_id: str) -> AppResponse:
        return AppResponse(
            True,
            {
                "status": {
                    "run_id": run_id,
                    "status": "running",
                    "current_partition": 3,
                    "paused": False,
                    "cancel_requested": False,
                },
                "progress": {
                    "discovered": 10,
                    "processed": 4,
                    "succeeded": 3,
                    "failed": 1,
                    "partitions": 2,
                },
                "failed_artifacts": ["failed.pdf"],
                "quarantined_artifacts": ["unsafe.html"],
                "pending_resolutions": [
                    {
                        "artifact_id": "policy.md",
                        "reason": "approval required",
                        "resume_stage": "finalize",
                    }
                ],
            },
        )

    async def control_ingestion(
        self,
        run_id: str,
        action: str,
    ) -> AppResponse:
        self.controls.append(action)
        return AppResponse(True, {"run_id": run_id, "action": action})


def test_dashboard_snapshot_normalizes_nested_progress() -> None:
    snapshot = DashboardSnapshot.from_payload(
        {
            "status": {
                "run_id": "run-1",
                "status": "paused",
                "progress": {"discovered": 7},
            }
        },
        fallback_run_id="fallback",
    )

    assert snapshot.run_id == "run-1"
    assert snapshot.status == "paused"
    assert snapshot.progress["discovered"] == 7


@pytest.mark.asyncio
async def test_dashboard_renders_live_status_and_controls_workflow() -> None:
    service = DashboardService()
    dashboard = IngestionDashboard("run-1", service, refresh_seconds=60)

    async with dashboard.run_test(size=(120, 42)) as pilot:
        await pilot.pause(0.1)

        assert dashboard.snapshot is not None
        assert dashboard.snapshot.status == "running"
        progress = dashboard.query_one("#progress", ProgressBar)
        assert progress.total == 10
        assert progress.progress == 4
        assert dashboard.query_one("#attention", DataTable).row_count == 3

        await pilot.press("p")
        await pilot.pause(0.1)
        assert "pause" in service.controls

        await pilot.press("x")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause(0.1)
        assert "cancel" in service.controls


@pytest.mark.asyncio
async def test_dashboard_logs_status_refresh_failures(caplog) -> None:
    class FailingDashboardService(DashboardService):
        async def ingestion_status(self, run_id: str) -> AppResponse:
            del run_id
            raise ConnectionError("private upstream detail")

    dashboard = IngestionDashboard("run-logging", FailingDashboardService(), refresh_seconds=60)
    with caplog.at_level(logging.ERROR, logger="harborrag.app.cli.dashboard"):
        async with dashboard.run_test(size=(120, 42)) as pilot:
            await pilot.pause(0.1)

    assert "Dashboard status refresh failed run_id=run-logging" in caplog.text
    assert "error_type=ConnectionError" in caplog.text
    assert "private upstream detail" not in caplog.text
