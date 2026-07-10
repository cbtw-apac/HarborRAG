from __future__ import annotations

import pytest
from harborrag_runtime.composition import CompositionRoot
from harborrag_runtime.job_state import InMemoryJobStore, JobState
from harborrag_runtime.jobs.base import BaseJobStore
from harborrag_runtime.local import run_sync
from harborrag_runtime.schedules import ScheduleSpec
from harborrag_runtime.scheduling.base import BaseScheduler
from harborrag_runtime.supervision.base import BaseSupervisor


class BrokenJobStore(BaseJobStore):
    def save(self, job):
        return super().save(job)

    def get(self, job_id):
        return super().get(job_id)


class BrokenScheduler(BaseScheduler):
    def add(self, schedule):
        return super().add(schedule)


class BrokenSupervisor(BaseSupervisor):
    def submit(self, job_id):
        return super().submit(job_id)


def test_runtime_base_methods_raise():
    with pytest.raises(NotImplementedError):
        BrokenJobStore().save(JobState("j"))
    with pytest.raises(NotImplementedError):
        BrokenJobStore().get("j")
    with pytest.raises(NotImplementedError):
        BrokenScheduler().add(ScheduleSpec("daily", "0 0 * * *"))
    with pytest.raises(NotImplementedError):
        BrokenSupervisor().submit("job")


def test_implemented_runtime_components():
    job = JobState("job-1")
    store = InMemoryJobStore()
    store.save(job)

    assert store.get("job-1") is job
    assert store.get("missing") is None
    assert run_sync(lambda: 42) == 42
    assert CompositionRoot.local().diagnostics()["engine"]["tenant"] == "default"
