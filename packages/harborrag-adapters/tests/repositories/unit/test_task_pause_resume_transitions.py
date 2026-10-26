"""Pause and resume move only a task's status, not its progress or start time."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harborrag_core.ingestion import IngestionTask, IngestionTaskState

from .ingestion_control_fixtures import make_control_plane

_PROGRESS = {
    "stage": "PROCESSING_DOCUMENTS",
    "discovered": 10_000,
    "admitted": 9_960,
    "discovery_pages": 40,
}


def _task(task_id: str) -> IngestionTask:
    return IngestionTask(
        task_id=task_id,
        source_scope_id="scope-engineering",
        status=IngestionTaskState.PENDING,
        request={"connector": "confluence"},
    )


@pytest.mark.asyncio
async def test_pause_and_resume_keep_the_summary_and_the_start_time(tmp_path: Path) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        task = _task("task-paused-mid-run")
        await control_plane.tasks.create(task)
        await control_plane.tasks.transition(task.task_id, IngestionTaskState.RUNNING)
        await control_plane.tasks.update_summary(task.task_id, _PROGRESS)
        started = await control_plane.tasks.get(task.task_id)
        assert started is not None
        assert started.started_at is not None

        await asyncio.sleep(0.01)
        await control_plane.tasks.transition(task.task_id, IngestionTaskState.PAUSED)
        paused = await control_plane.tasks.get(task.task_id)
        await control_plane.tasks.transition(task.task_id, IngestionTaskState.RUNNING)
        resumed = await control_plane.tasks.get(task.task_id)

        assert paused is not None
        assert resumed is not None
        assert paused.status is IngestionTaskState.PAUSED
        assert paused.summary == _PROGRESS
        assert resumed.status is IngestionTaskState.RUNNING
        assert resumed.summary == _PROGRESS
        assert resumed.started_at == started.started_at


@pytest.mark.asyncio
async def test_an_explicit_summary_still_replaces_the_stored_one(tmp_path: Path) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        task = _task("task-failed-mid-run")
        await control_plane.tasks.create(task)
        await control_plane.tasks.transition(task.task_id, IngestionTaskState.RUNNING)
        await control_plane.tasks.update_summary(task.task_id, _PROGRESS)

        await control_plane.tasks.transition(
            task.task_id,
            IngestionTaskState.FAILED,
            summary={"failed_stage": "document_dispatch"},
        )

        failed = await control_plane.tasks.get(task.task_id)
        assert failed is not None
        assert failed.summary == {"failed_stage": "document_dispatch"}
        assert failed.completed_at is not None
