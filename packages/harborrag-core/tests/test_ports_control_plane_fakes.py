"""Fakes for the control-plane/jobs/secrets/events ports behave like real impls (ST6)."""

import pytest
from core_test_fixtures import (
    FakeEventBus,
    FakeJobQueue,
    FakeProjectRepository,
    FakeSecrets,
)

from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.job import Job
from harborrag_core.domain.project import Project


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_fake_project_repository_crud_roundtrip() -> None:
    """Create/get/list/update/delete round-trip on the dict-backed fake."""
    repo = FakeProjectRepository()
    project = Project(id="p1", name="Docs", collection="docs_main")
    await repo.create(project)
    assert await repo.get("p1") == project
    assert await repo.list() == [project]
    project.name = "Docs v2"
    await repo.update(project)
    fetched = await repo.get("p1")
    assert fetched is not None and fetched.name == "Docs v2"
    await repo.delete("p1")
    assert await repo.get("p1") is None


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_fake_job_queue_lifecycle() -> None:
    """enqueue -> claim (running, attempts+1) -> done/failed/cancel transitions,
    with retryable failures re-queued (plan ST6 queue semantics preview)."""
    queue = FakeJobQueue()
    job = Job(id="j1", source_id="s1", project_id="p1", job_type="bulk_ingest")
    await queue.enqueue(job)

    claimed = await queue.claim_next(lease_seconds=30)
    assert claimed is not None
    assert claimed.status == "running" and claimed.attempts == 1
    assert await queue.claim_next(lease_seconds=30) is None  # nothing else queued

    await queue.mark_failed("j1", error="boom", retryable=True)
    assert queue.jobs["j1"].status == "queued"  # retryable -> back in queue

    reclaimed = await queue.claim_next(lease_seconds=30)
    assert reclaimed is not None and reclaimed.attempts == 2
    await queue.mark_done("j1")
    assert queue.jobs["j1"].status == "succeeded"

    job2 = Job(id="j2", source_id="s1", project_id="p1", job_type="dry_run")
    await queue.enqueue(job2)
    await queue.cancel("j2")
    assert queue.jobs["j2"].status == "cancelled"
    assert await queue.claim_next(lease_seconds=30) is None  # cancelled never claimed


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_fake_secrets_put_resolve_delete() -> None:
    """put returns an opaque ref (never echoing the value); resolve returns
    the value; delete forgets it."""
    secrets = FakeSecrets()
    ref = await secrets.put("hunter2")
    assert "hunter2" not in ref
    assert await secrets.resolve(ref) == "hunter2"
    await secrets.delete(ref)
    with pytest.raises(KeyError):
        await secrets.resolve(ref)


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_fake_event_bus_publish_and_filtered_subscribe() -> None:
    """subscribe(name_prefix) yields only matching already-published events,
    in publish order (deterministic replay for tests)."""
    bus = FakeEventBus()
    await bus.publish(HarborEvent(name="job_status", trace_id="t1"))
    await bus.publish(HarborEvent(name="metrics", trace_id="t2"))
    await bus.publish(HarborEvent(name="job_progress", trace_id="t3"))

    seen = [event.name async for event in bus.subscribe("job")]
    assert seen == ["job_status", "job_progress"]
