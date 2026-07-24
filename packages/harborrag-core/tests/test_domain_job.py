"""Core job + source-config aggregates promoted from runtime."""

from datetime import UTC
from typing import get_args

import pytest

from harborrag_core.domain.job import Job, JobCounters, JobStatus, JobType
from harborrag_core.domain.source_config import SourceConfig


@pytest.mark.whitebox
def test_job_status_includes_cancelled() -> None:
    """JobStatus covers the five API statuses from the plan (§5 conventions),
    including the newly added `cancelled`."""
    assert set(get_args(JobStatus)) == {
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    }


@pytest.mark.whitebox
def test_job_type_literals() -> None:
    """JobType matches the three M0 job types from the plan (§5.3)."""
    assert set(get_args(JobType)) == {"bulk_ingest", "incremental_pull", "dry_run"}


@pytest.mark.whitebox
def test_job_defaults() -> None:
    """A fresh Job is queued with zeroed counters, no attempts, and a
    timezone-aware UTC enqueue timestamp."""
    job = Job(id="j1", source_id="s1", project_id="p1", job_type="bulk_ingest")
    assert job.status == "queued"
    assert job.attempts == 0
    assert job.dry_run is False
    assert job.enqueued_at.tzinfo is UTC
    assert job.started_at is None and job.finished_at is None
    assert job.last_error is None
    assert job.payload == {}
    assert job.counters == JobCounters()


@pytest.mark.whitebox
def test_job_counters_default_to_zero() -> None:
    """JobCounters starts at zero for all three counters the API exposes."""
    counters = JobCounters()
    assert (counters.documents_processed, counters.chunks_created, counters.errors) == (
        0,
        0,
        0,
    )


@pytest.mark.whitebox
def test_source_config_defaults() -> None:
    """A fresh SourceConfig is active, unscheduled, and carries only
    secret_ref placeholders (never secret values)."""
    source = SourceConfig(id="s1", project_id="p1", source_type="local_file", name="docs")
    assert source.status == "active"
    assert source.schedule is None
    assert source.config == {}
    assert source.secret_refs == []


@pytest.mark.whitebox
@pytest.mark.parametrize("bad_id", ["", "   ", "has space"])
def test_job_and_source_config_reject_blank_or_whitespace_ids(bad_id: str) -> None:
    """A blank/whitespace id must never construct a Job or SourceConfig --
    it would otherwise flow uncaught into the queue/repository layers."""
    with pytest.raises(ValueError, match="id must be non-empty"):
        Job(id=bad_id, source_id="s1", project_id="p1", job_type="bulk_ingest")
    with pytest.raises(ValueError, match="id must be non-empty"):
        SourceConfig(id=bad_id, project_id="p1", source_type="local_file", name="docs")
