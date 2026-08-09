"""Concurrency guarantees for the append-only control-plane job event log."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.jobs import SqlJobRepository
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.contracts.errors import HarborConflictError, HarborNotFoundError
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.job import Job

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[SessionFactory]:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_job_repository_roundtrip_and_event_log(
    sessions: SessionFactory,
) -> None:
    """Job save/get/list filters + ordered per-job event sequence numbers."""
    repo = SqlJobRepository(sessions)
    job = Job(
        id="j1",
        tenant_id="tenant-a",
        source_id="s1",
        project_id="p1",
        job_type="bulk_ingest",
    )
    await repo.save(job)
    assert await repo.get("j1") == job
    job.status = "running"
    job.attempts = 1
    await repo.save(job)
    assert [j.id for j in await repo.list(status="running")] == ["j1"]
    assert await repo.list(status="failed") == []
    assert [j.id for j in await repo.list(source_id="s1")] == ["j1"]
    job.tenant_id = "tenant-b"
    with pytest.raises(HarborConflictError, match="tenant identity is immutable"):
        await repo.save(job)
    job.tenant_id = "tenant-a"

    await repo.append_event(
        "j1", HarborEvent(name="job_status", trace_id="t1", payload={"s": "running"})
    )
    await repo.append_event("j1", HarborEvent(name="job_status", trace_id="t2"))
    async with sessions() as session:
        seqs = list(
            await session.scalars(
                sa.text("SELECT seq FROM job_events WHERE job_id='j1' ORDER BY seq")
            )
        )
    assert seqs == [1, 2]


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_job_repository_allocates_event_sequences_atomically(
    sessions: SessionFactory,
) -> None:
    repo = SqlJobRepository(sessions)
    await repo.save(
        Job(
            id="j1",
            tenant_id="DEFAULT",
            source_id="s1",
            project_id="p1",
            job_type="bulk_ingest",
        )
    )

    await asyncio.gather(
        *(
            repo.append_event("j1", HarborEvent(name="job_status", trace_id=f"t{index}"))
            for index in range(20)
        )
    )

    async with sessions() as session:
        seqs = list(
            await session.scalars(
                sa.text("SELECT seq FROM job_events WHERE job_id='j1' ORDER BY seq")
            )
        )
    assert seqs == list(range(1, 21))


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_job_repository_rejects_event_for_unknown_job(
    sessions: SessionFactory,
) -> None:
    repo = SqlJobRepository(sessions)

    with pytest.raises(HarborNotFoundError, match="job does not exist"):
        await repo.append_event("missing", HarborEvent(name="job_status", trace_id="trace"))
