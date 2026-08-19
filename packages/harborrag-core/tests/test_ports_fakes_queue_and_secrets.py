"""Contract tests for FakeJobQueue, FakeSecrets, and FakeEventBus.

Split out of test_ports_control_plane_fakes.py (file-length gate).
"""

from __future__ import annotations

import pytest

from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.job import Job
from harborrag_core.testing.fakes import FakeEventBus, FakeJobQueue, FakeSecrets

pytestmark = pytest.mark.whitebox


def _job(job_id: str, *, source_id: str = "s1", status: str = "queued") -> Job:
    return Job(
        id=job_id,
        tenant_id="DEFAULT",
        source_id=source_id,
        project_id="p1",
        job_type="bulk_ingest",
        status=status,  # type: ignore[arg-type]
    )


def _status(job: Job) -> str:
    """Read mutable status without narrowing it across repository calls."""
    return job.status


@pytest.mark.asyncio
async def test_fake_job_queue_handles_skips_retries_failures_and_cancellation() -> None:
    queue = FakeJobQueue()
    already_done = _job("already-done", status="succeeded")
    retrying = _job("retrying")
    await queue.enqueue(already_done)
    assert await queue.enqueue(retrying) is retrying

    assert await queue.claim_next(lease_seconds=30) is retrying
    assert _status(retrying) == "running" and retrying.attempts == 1
    await queue.mark_failed(retrying.id, "temporary", retryable=True)
    assert _status(retrying) == "queued" and retrying.last_error == "temporary"
    assert await queue.claim_next(lease_seconds=30) is retrying
    await queue.mark_failed(retrying.id, "permanent", retryable=False)
    assert _status(retrying) == "failed" and retrying.last_error == "permanent"

    successful = _job("successful")
    await queue.enqueue(successful)
    assert await queue.claim_next(lease_seconds=30) is successful
    await queue.mark_done(successful.id)
    assert _status(successful) == "succeeded"

    cancelled = _job("cancelled")
    await queue.enqueue(cancelled)
    await queue.cancel(cancelled.id)
    assert _status(cancelled) == "cancelled"
    assert await queue.claim_next(lease_seconds=30) is None


@pytest.mark.asyncio
async def test_fake_secrets_and_event_bus_are_opaque_and_deterministic() -> None:
    secrets = FakeSecrets()
    first_ref = await secrets.put("alpha")
    second_ref = await secrets.put("beta")
    assert (first_ref, second_ref) == ("secret://fake/1", "secret://fake/2")
    assert await secrets.resolve(first_ref) == "alpha"
    await secrets.delete(first_ref)
    await secrets.delete("secret://fake/missing")
    with pytest.raises(KeyError):
        await secrets.resolve(first_ref)

    bus = FakeEventBus()
    started = HarborEvent(name="job.started", trace_id="trace-1")
    finished = HarborEvent(name="job.finished", trace_id="trace-2")
    ignored = HarborEvent(name="source.created", trace_id="trace-3")
    for event in (started, finished, ignored):
        await bus.publish(event)
    assert [event async for event in bus.subscribe("job.")] == [started, finished]
    assert [event async for event in bus.subscribe("missing.")] == []
