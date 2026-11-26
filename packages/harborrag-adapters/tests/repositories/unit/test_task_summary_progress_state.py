"""A task summary replaced from an earlier read never rolls back the progress bridge state."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.ingestion_control import IngestionTaskRepository
from harborrag_adapters.repositories.database.ingestion_control.task_lifecycle import (
    PROGRESS_STATE_KEYS,
)
from harborrag_core.ingestion import IngestionTask, IngestionTaskState

from .ingestion_control_fixtures import make_control_plane

pytestmark = pytest.mark.unit

_SNAPSHOT_KEY, _CURSOR_KEY = PROGRESS_STATE_KEYS
_Replace = Callable[[IngestionTaskRepository, dict[str, object]], Awaitable[None]]


async def _finalize(tasks: IngestionTaskRepository, summary: dict[str, object]) -> None:
    await tasks.finalize("task-1", IngestionTaskState.PARTIAL, summary=summary)


async def _fail(tasks: IngestionTaskRepository, summary: dict[str, object]) -> None:
    await tasks.transition("task-1", IngestionTaskState.FAILED, summary=summary)


@pytest.mark.asyncio
@pytest.mark.parametrize("replace", [_finalize, _fail], ids=["finalize", "transition"])
async def test_a_stale_replacement_keeps_the_newer_stored_progress_state(
    tmp_path: Path, replace: _Replace
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        tasks = control_plane.tasks
        await tasks.create(
            IngestionTask(
                task_id="task-1",
                source_scope_id="scope-docs",
                status=IngestionTaskState.PENDING,
                request={"connector_type": "local"},
            )
        )
        await tasks.transition("task-1", IngestionTaskState.RUNNING)
        await tasks.update_summary(
            "task-1",
            {_SNAPSHOT_KEY: {"status": "running"}, _CURSOR_KEY: ["2026-01-01T00:00:00", "d-1"]},
        )
        stale = await tasks.get("task-1")
        assert stale is not None
        await tasks.update_summary(
            "task-1",
            {_SNAPSHOT_KEY: {"status": "running", "failed": 2}, _CURSOR_KEY: ["2026-01-02", "d-2"]},
        )

        await replace(tasks, {"stage": "COMPLETED", **stale.summary})
        ended = await tasks.get("task-1")

    assert ended is not None
    assert ended.summary["stage"] == "COMPLETED"
    assert ended.summary[_SNAPSHOT_KEY] == {"status": "running", "failed": 2}
    assert ended.summary[_CURSOR_KEY] == ["2026-01-02", "d-2"]
