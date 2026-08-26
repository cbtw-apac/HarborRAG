"""Concurrency guarantees for the append-only ingestion task event log.

Mirrors test_job_event_concurrency.py's coverage for the job domain's
job_events table, retargeted at task_events (see 0015_ingestion_task_events).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.ingestion import IngestionTask, IngestionTaskState

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def control(tmp_path: Path) -> AsyncIterator[IngestionControlPlaneDatabase]:
    client = SQLAlchemyDBClient(
        backend="sqlite",
        url=f"sqlite+aiosqlite:///{tmp_path / 'ingestion-events.db'}",
        pool_size=None,
        max_overflow=None,
        pool_recycle_seconds=1_800,
        echo=False,
    )
    database = IngestionControlPlaneDatabase(client, create_schema=True)
    await database.connect()
    yield database
    await database.close()


async def _seed_task(control: IngestionControlPlaneDatabase, task_id: str) -> None:
    await control.tasks.register(
        IngestionTask(
            task_id=task_id,
            source_scope_id="scope-1",
            status=IngestionTaskState.PENDING,
            request={"tenant_id": "DEFAULT", "connector_type": "local", "connection_id": "c1"},
        )
    )


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_task_event_log_orders_by_sequence_and_is_bounded(
    control: IngestionControlPlaneDatabase,
) -> None:
    await _seed_task(control, "task-1")

    await control.task_events.append_event(
        "task-1", HarborEvent(name="task.task-1.progress", trace_id="t1", payload={"n": 1})
    )
    await control.task_events.append_event(
        "task-1", HarborEvent(name="task.task-1.progress", trace_id="t2", payload={"n": 2})
    )
    await control.task_events.append_event(
        "task-1", HarborEvent(name="task.task-1.done", trace_id="t3", payload={"n": 3})
    )

    events = await control.task_events.list_events("task-1")
    assert [event.trace_id for event in events] == ["t1", "t2", "t3"]

    bounded = await control.task_events.list_events("task-1", limit=1)
    assert [event.trace_id for event in bounded] == ["t1"]

    resumed = await control.task_events.list_events("task-1", after_seq=1, limit=1)
    assert [event.trace_id for event in resumed] == ["t2"]


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_task_event_sequences_are_allocated_atomically(
    control: IngestionControlPlaneDatabase,
) -> None:
    await _seed_task(control, "task-1")

    await asyncio.gather(
        *(
            control.task_events.append_event(
                "task-1", HarborEvent(name="task.task-1.progress", trace_id=f"t{index}")
            )
            for index in range(20)
        )
    )

    events = await control.task_events.list_events("task-1", limit=100)
    assert len(events) == 20
    assert len({event.trace_id for event in events}) == 20


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_task_event_rejects_append_for_unknown_task(
    control: IngestionControlPlaneDatabase,
) -> None:
    with pytest.raises(HarborNotFoundError, match="ingestion task does not exist"):
        await control.task_events.append_event(
            "missing", HarborEvent(name="task.missing.progress", trace_id="trace")
        )
