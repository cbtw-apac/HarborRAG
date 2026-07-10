from __future__ import annotations

import pytest
from harborrag_runtime.composition import CompositionRoot
from harborrag_runtime.job_state import InMemoryJobStore, JobState
from harborrag_runtime.jobs.base import BaseJobStore
from harborrag_runtime.jobs.mock import MockJobStore
from harborrag_runtime.local import run_sync
from harborrag_runtime.schedules import ScheduleSpec
from harborrag_runtime.scheduling.base import BaseScheduler
from harborrag_runtime.scheduling.mock import MockScheduler
from harborrag_runtime.services.mock import MockRuntimeService
from harborrag_runtime.supervision.base import BaseSupervisor
from harborrag_runtime.supervision.mock import MockSupervisor
from harborrag_runtime.supervisor import LocalSupervisor


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


def test_mock_job_store_supervisor_and_scheduler():
    job_store = MockJobStore()
    job = JobState("job-1")
    job_store.save(job)
    assert job_store.get("job-1") is job
    assert job_store.get("missing") is None

    supervisor = MockSupervisor()
    supervisor.submit("job-1")
    assert supervisor.submitted == ["job-1"]

    scheduler = MockScheduler()
    spec = ScheduleSpec("daily", "0 0 * * *")
    scheduler.add(spec)
    assert scheduler.schedules == [spec]


def test_local_supervisor_tracks_running_jobs():
    supervisor = LocalSupervisor()
    supervisor.start("job-1")
    assert "job-1" in supervisor.running_jobs
    supervisor.finish("job-1")
    assert "job-1" not in supervisor.running_jobs
    supervisor.finish("never-started")


def test_mock_runtime_service_diagnostics_and_ingestion():
    service = MockRuntimeService()

    assert service.diagnostics() == {"provider": "mock_runtime", "ready": True}

    result = service.run_mock_ingestion()

    assert result["documents"] == ["mock://composition/1"]
    assert result["summary"] == {
        "discovered": 1,
        "loaded": 1,
        "parsed": 1,
        "indexed": 0,
    }


def test_composition_root_mock_pipeline_runs_connector_through_parser():
    root = CompositionRoot.local()
    pipeline = root.mock_pipeline()

    documents = pipeline.run_once()

    assert [doc.id for doc in documents] == ["mock://composition/1"]
    summary = pipeline.summarize()
    assert (summary.discovered, summary.loaded, summary.parsed) == (1, 1, 1)


def test_package_lazily_loads_composition_root_and_rejects_unknown_attrs():
    import harborrag_runtime

    assert harborrag_runtime.CompositionRoot is CompositionRoot
    with pytest.raises(AttributeError, match="no attribute 'missing'"):
        _ = harborrag_runtime.missing
