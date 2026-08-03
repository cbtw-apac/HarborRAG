"""GET /jobs/{id}/stream (SSE): job progress streaming (ML2 P3)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.dependencies import get_app_service
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.client import AppService
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
    def __init__(self) -> None:
        self.execution_status_value = "running"

    async def start_ingestion(self, request):
        return RuntimeWorkflowRef(request.run_id, "wf-1", "execution-1")

    async def result(self, run_id: str):
        return {"status": "completed"}

    async def get_status(self, run_id: str):
        return {"run_id": run_id, "status": "running"}

    async def execution_status(self, run_id: str) -> str:
        return self.execution_status_value

    async def get_progress(self, run_id: str):
        return {"discovered": 1, "processed": 1}

    async def get_failed_artifacts(self, run_id: str):
        return []

    async def get_quarantined_artifacts(self, run_id: str):
        return []

    async def get_pending_resolutions(self, run_id: str):
        return []

    async def cancel(self, run_id: str, *, graceful: bool) -> None:
        return None


def _service(*, sources: list[SourceConfig] | None = None) -> AppService:
    control_plane = ControlPlaneRepositories(
        projects=FakeProjectRepository({"proj-a": Project(id="proj-a", name="A", collection="a")}),
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
        return FakeRuntimeClient()

    return AppService(composition, client_factory=client_factory)


def _local_file_source() -> SourceConfig:
    return SourceConfig(
        id="src-1",
        project_id="proj-a",
        source_type="local_file",
        name="Docs",
        config={"path": "./docs"},
    )


@pytest.mark.blackbox
def test_stream_replays_backlog_and_closes_for_an_already_terminal_job() -> None:
    """Deterministic, bounded case: the job reaches a terminal state before
    the stream ever opens, so stream_job_events short-circuits after backlog
    replay instead of blocking on a live tail -- safe for a plain (non-
    streaming) TestClient GET."""
    source = _local_file_source()
    app = create_fastapi_app(ApiSettings())
    service = _service(sources=[source])
    app.dependency_overrides[get_app_service] = lambda: service
    with TestClient(app) as client:
        response = client.post(f"/api/v1/sources/{source.id}/jobs", json={})
        job_id = response.json()["job"]["id"]

        service._client.execution_status_value = "completed"
        asyncio.run(service.sync_job_progress())

        stream_response = client.get(f"/api/v1/jobs/{job_id}/stream")
        assert stream_response.status_code == 200
        assert stream_response.headers["content-type"].startswith("text/event-stream")
        body = stream_response.text
        assert f"event: job.{job_id}.progress" in body
        assert f"event: job.{job_id}.done" in body


@pytest.mark.blackbox
def test_stream_unknown_job_returns_enveloped_404() -> None:
    app = create_fastapi_app(ApiSettings())
    service = _service()
    app.dependency_overrides[get_app_service] = lambda: service
    with TestClient(app) as client:
        response = client.get("/api/v1/jobs/does-not-exist/stream")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "harbor_not_found_error"
