"""Source -> Job bridge (ML2 P2): create/list/get/result/control_job semantics."""

from __future__ import annotations

import pytest

from harborrag_app.workflow_control.client import AppService
from harborrag_core.contracts.errors import (
    HarborCapabilityError,
    HarborConflictError,
    HarborNotFoundError,
)
from harborrag_core.domain.project import Project
from harborrag_core.domain.source_config import SourceConfig
from harborrag_core.testing.fakes import (
    FakeActivityRepository,
    FakeJobRepository,
    FakeMemberRepository,
    FakeProjectRepository,
    FakeProviderRepository,
    FakeSecrets,
    FakeSettingsRepository,
    FakeSourceRepository,
)
from harborrag_runtime.composition import (
    CompositionRoot,
    ControlPlaneRepositories,
    build_in_process_event_bus,
)
from harborrag_runtime.temporal.identity import RuntimeWorkflowRef


class FakeRuntimeClient:
    """Records calls and can be told to fail every operation.

    execution_status_value is mutable so a test can change what the next
    poll tick sees (e.g. "running" -> "completed") without rebuilding the
    client -- AppService caches the client instance across calls.
    """

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self._failure = failure
        self.execution_status_value = "running"

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        if self._failure is not None:
            raise self._failure

    async def start_ingestion(self, request):
        self._record("start_ingestion", request)
        return RuntimeWorkflowRef(request.run_id, "wf-1", "execution-1")

    async def result(self, run_id: str):
        self._record("result", run_id)
        return {"status": "completed"}

    async def get_status(self, run_id: str):
        self._record("get_status", run_id)
        return {"run_id": run_id, "status": "running"}

    async def execution_status(self, run_id: str) -> str:
        self._record("execution_status", run_id)
        return self.execution_status_value

    async def get_progress(self, run_id: str):
        self._record("get_progress", run_id)
        return {"discovered": 2, "processed": 1}

    async def get_failed_artifacts(self, run_id: str):
        self._record("get_failed_artifacts", run_id)
        return []

    async def get_quarantined_artifacts(self, run_id: str):
        self._record("get_quarantined_artifacts", run_id)
        return []

    async def get_pending_resolutions(self, run_id: str):
        self._record("get_pending_resolutions", run_id)
        return []

    async def pause(self, run_id: str) -> None:
        self._record("pause", run_id)

    async def resume(self, run_id: str) -> None:
        self._record("resume", run_id)

    async def cancel(self, run_id: str, *, graceful: bool) -> None:
        self._record("cancel", run_id, graceful)

    async def retry_failed(self, run_id: str, artifact_ids) -> None:
        self._record("retry_failed", run_id, tuple(artifact_ids))


def _build_service(
    *,
    projects: list[Project] | None = None,
    sources: list[SourceConfig] | None = None,
    failure: Exception | None = None,
) -> AppService:
    control_plane = ControlPlaneRepositories(
        projects=FakeProjectRepository({project.id: project for project in projects or []}),
        sources=FakeSourceRepository({source.id: source for source in sources or []}),
        jobs=FakeJobRepository(),
        activity=FakeActivityRepository(),
        settings=FakeSettingsRepository(),
        providers=FakeProviderRepository(),
        members=FakeMemberRepository(),
        secrets=FakeSecrets(),
    )
    composition = CompositionRoot(
        control_plane=control_plane,
        event_bus=build_in_process_event_bus(),
        control_db={"ping": "ok", "scheme": "development"},
        mode="development",
    )

    async def client_factory(_config: object) -> FakeRuntimeClient:
        return FakeRuntimeClient(failure=failure)

    return AppService(composition, client_factory=client_factory)


def _local_file_source(project_id: str = "proj-a") -> SourceConfig:
    return SourceConfig(
        id="src-1",
        project_id=project_id,
        source_type="local_file",
        name="Docs",
        config={"path": "./docs"},
    )


@pytest.mark.asyncio
async def test_create_job_starts_ingestion_and_persists_running() -> None:
    project = Project(id="proj-a", name="A", collection="a")
    source = _local_file_source()
    service = _build_service(projects=[project], sources=[source])

    response = await service.create_job(source.id, actor="alice@example.com")

    assert response.ok
    job = response.data["job"]
    assert job.status == "running"
    assert job.source_id == source.id
    assert job.project_id == "proj-a"
    assert "run" in response.data
    assert "workflow" in response.data

    control_plane = service._control_plane()
    stored = await control_plane.jobs.get(job.id)
    assert stored is not None
    assert stored.status == "running"
    [entry] = await control_plane.activity.list()
    assert entry.actor == "alice@example.com"
    assert entry.verb == "created"
    assert entry.entity_type == "job"


@pytest.mark.asyncio
async def test_create_job_unknown_source_raises_not_found() -> None:
    service = _build_service()
    with pytest.raises(HarborNotFoundError):
        await service.create_job("ghost", actor="alice@example.com")


@pytest.mark.asyncio
async def test_create_job_rejects_unsupported_source_type() -> None:
    project = Project(id="proj-a", name="A", collection="a")
    source = SourceConfig(
        id="src-1", project_id="proj-a", source_type="notion", name="Notion space"
    )
    service = _build_service(projects=[project], sources=[source])
    with pytest.raises(HarborCapabilityError):
        await service.create_job(source.id, actor="alice@example.com")


@pytest.mark.asyncio
async def test_create_job_rejects_a_run_id_already_used_by_another_job() -> None:
    project = Project(id="proj-a", name="A", collection="a")
    source = _local_file_source()
    service = _build_service(projects=[project], sources=[source])
    existing = (
        await service.create_job(source.id, run_id="job-dup", actor="alice@example.com")
    ).data["job"]
    assert existing.status == "running"

    with pytest.raises(HarborConflictError):
        await service.create_job(source.id, run_id="job-dup", actor="alice@example.com")

    control_plane = service._control_plane()
    stored = await control_plane.jobs.get("job-dup")
    assert stored is not None
    assert stored.status == "running"
    start_calls = [call for call in service._client.calls if call[0] == "start_ingestion"]
    assert len(start_calls) == 1


@pytest.mark.asyncio
async def test_create_job_persists_failed_when_temporal_start_fails() -> None:
    project = Project(id="proj-a", name="A", collection="a")
    source = _local_file_source()
    service = _build_service(projects=[project], sources=[source], failure=RuntimeError("boom"))

    response = await service.create_job(source.id, actor="alice@example.com")

    assert not response.ok
    job = response.data["job"]
    assert job.status == "failed"
    assert job.last_error is not None

    control_plane = service._control_plane()
    stored = await control_plane.jobs.get(job.id)
    assert stored is not None
    assert stored.status == "failed"


@pytest.mark.asyncio
async def test_list_jobs_filters_by_source_and_status() -> None:
    project = Project(id="proj-a", name="A", collection="a")
    source_a = _local_file_source()
    source_b = SourceConfig(
        id="src-2", project_id="proj-a", source_type="local_file", name="Other docs"
    )
    service = _build_service(projects=[project], sources=[source_a, source_b])

    await service.create_job(source_a.id, actor="alice@example.com")
    await service.create_job(source_b.id, actor="alice@example.com")

    all_jobs = (await service.list_jobs()).data["jobs"]
    assert len(all_jobs) == 2

    scoped = (await service.list_jobs(source_id=source_a.id)).data["jobs"]
    assert [job.source_id for job in scoped] == [source_a.id]

    running = (await service.list_jobs(status="running")).data["jobs"]
    assert len(running) == 2


@pytest.mark.asyncio
async def test_get_job_merges_persisted_row_with_live_temporal_state() -> None:
    project = Project(id="proj-a", name="A", collection="a")
    source = _local_file_source()
    service = _build_service(projects=[project], sources=[source])
    created = (await service.create_job(source.id, actor="alice@example.com")).data["job"]

    response = await service.get_job(created.id)

    assert response.ok
    assert response.data["job"].id == created.id
    assert response.data["live"]["execution_status"] == "running"


@pytest.mark.asyncio
async def test_get_job_unknown_id_raises_not_found() -> None:
    service = _build_service()
    with pytest.raises(HarborNotFoundError):
        await service.get_job("ghost")


@pytest.mark.asyncio
async def test_get_job_result_merges_persisted_row_with_result() -> None:
    project = Project(id="proj-a", name="A", collection="a")
    source = _local_file_source()
    service = _build_service(projects=[project], sources=[source])
    created = (await service.create_job(source.id, actor="alice@example.com")).data["job"]

    response = await service.get_job_result(created.id)

    assert response.ok
    assert response.data["job"].id == created.id
    assert response.data["result"] == {"status": "completed"}


@pytest.mark.asyncio
async def test_control_job_cancel_persists_cancelled_and_logs_activity() -> None:
    project = Project(id="proj-a", name="A", collection="a")
    source = _local_file_source()
    service = _build_service(projects=[project], sources=[source])
    created = (await service.create_job(source.id, actor="alice@example.com")).data["job"]

    response = await service.control_job(created.id, "cancel", actor="bob@example.com")

    assert response.ok
    assert response.data["job"].status == "cancelled"
    control_plane = service._control_plane()
    stored = await control_plane.jobs.get(created.id)
    assert stored is not None
    assert stored.status == "cancelled"
    verbs = [entry.verb for entry in await control_plane.activity.list()]
    assert verbs == ["cancel", "created"]


@pytest.mark.asyncio
async def test_control_job_unknown_id_raises_not_found() -> None:
    service = _build_service()
    with pytest.raises(HarborNotFoundError):
        await service.control_job("ghost", "cancel", actor="alice@example.com")


@pytest.mark.asyncio
async def test_sync_job_progress_publishes_and_persists_on_first_observed_snapshot() -> None:
    project = Project(id="proj-a", name="A", collection="a")
    source = _local_file_source()
    service = _build_service(projects=[project], sources=[source])
    job = (await service.create_job(source.id, actor="alice@example.com")).data["job"]

    synced = await service.sync_job_progress()

    assert synced.data == {"synced": 1}
    control_plane = service._control_plane()
    events = await control_plane.jobs.list_events(job.id)
    assert [event.name for event in events] == [f"job.{job.id}.progress"]
    stored = await control_plane.jobs.get(job.id)
    assert stored is not None
    assert stored.payload["_last_progress"] is not None


@pytest.mark.asyncio
async def test_sync_job_progress_updates_counters_from_live_progress() -> None:
    project = Project(id="proj-a", name="A", collection="a")
    source = _local_file_source()
    service = _build_service(projects=[project], sources=[source])
    job = (await service.create_job(source.id, actor="alice@example.com")).data["job"]
    assert job.counters.documents_processed == 0

    await service.sync_job_progress()

    stored = await service._control_plane().jobs.get(job.id)
    assert stored is not None
    # FakeRuntimeClient.get_progress returns {"discovered": 2, "processed": 1}.
    assert stored.counters.documents_processed == 1
    assert stored.counters.errors == 0


@pytest.mark.asyncio
async def test_sync_job_progress_publishes_nothing_when_snapshot_unchanged() -> None:
    project = Project(id="proj-a", name="A", collection="a")
    source = _local_file_source()
    service = _build_service(projects=[project], sources=[source])
    job = (await service.create_job(source.id, actor="alice@example.com")).data["job"]
    await service.sync_job_progress()

    await service.sync_job_progress()

    events = await service._control_plane().jobs.list_events(job.id)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_sync_job_progress_terminal_status_updates_job_and_emits_done() -> None:
    project = Project(id="proj-a", name="A", collection="a")
    source = _local_file_source()
    service = _build_service(projects=[project], sources=[source])
    job = (await service.create_job(source.id, actor="alice@example.com")).data["job"]
    await service.sync_job_progress()

    service._client.execution_status_value = "completed"
    await service.sync_job_progress()

    control_plane = service._control_plane()
    stored = await control_plane.jobs.get(job.id)
    assert stored is not None
    assert stored.status == "succeeded"
    events = await control_plane.jobs.list_events(job.id)
    assert [event.name for event in events] == [
        f"job.{job.id}.progress",
        f"job.{job.id}.progress",
        f"job.{job.id}.done",
    ]
    # Terminal jobs drop out of the next tick's "running" sweep.
    still_running = await control_plane.jobs.list(status="running")
    assert still_running == []


@pytest.mark.asyncio
async def test_sync_job_progress_skips_unreachable_job_without_raising() -> None:
    project = Project(id="proj-a", name="A", collection="a")
    source = _local_file_source()
    service = _build_service(projects=[project], sources=[source], failure=RuntimeError("boom"))
    # Force the job into "running" directly (bypassing create_job's own
    # failure handling) so sync_job_progress has a running job whose live
    # Temporal calls fail.
    job = (await service.create_job(source.id, actor="alice@example.com")).data["job"]
    job.status = "running"
    await service._control_plane().jobs.save(job)

    synced = await service.sync_job_progress()

    assert synced.ok
    events = await service._control_plane().jobs.list_events(job.id)
    assert events == []


@pytest.mark.asyncio
async def test_stream_job_events_yields_backlog_then_live_and_stops_after_done() -> None:
    project = Project(id="proj-a", name="A", collection="a")
    source = _local_file_source()
    service = _build_service(projects=[project], sources=[source])
    job = (await service.create_job(source.id, actor="alice@example.com")).data["job"]
    await service.sync_job_progress()

    stream = service.stream_job_events(job.id)
    backlog_event = await stream.__anext__()
    assert backlog_event.name == f"job.{job.id}.progress"

    service._client.execution_status_value = "completed"
    await service.sync_job_progress()

    live_progress = await stream.__anext__()
    assert live_progress.name == f"job.{job.id}.progress"
    live_done = await stream.__anext__()
    assert live_done.name == f"job.{job.id}.done"

    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


@pytest.mark.asyncio
async def test_stream_job_events_unknown_job_raises_not_found() -> None:
    service = _build_service()
    with pytest.raises(HarborNotFoundError):
        await service.stream_job_events("ghost").__anext__()


@pytest.mark.asyncio
async def test_stream_job_events_short_circuits_for_already_terminal_job() -> None:
    project = Project(id="proj-a", name="A", collection="a")
    source = _local_file_source()
    service = _build_service(projects=[project], sources=[source], failure=RuntimeError("boom"))
    job = (await service.create_job(source.id, actor="alice@example.com")).data["job"]
    assert job.status == "failed"

    stream = service.stream_job_events(job.id)
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
