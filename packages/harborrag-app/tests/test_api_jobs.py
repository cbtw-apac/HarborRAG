"""Job endpoints (ML2 P2) over the mock app service + a fake Temporal client."""

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
from harborrag_runtime.composition import CompositionRoot, ControlPlaneRepositories
from harborrag_runtime.temporal.identity import RuntimeWorkflowRef


class FakeRuntimeClient:
    async def start_ingestion(self, request):
        return RuntimeWorkflowRef(request.run_id, "wf-1", "execution-1")

    async def result(self, run_id: str):
        return {"status": "completed"}

    async def get_status(self, run_id: str):
        return {"run_id": run_id, "status": "running"}

    async def execution_status(self, run_id: str) -> str:
        return "running"

    async def get_progress(self, run_id: str):
        return {"discovered": 1, "processed": 1}

    async def get_failed_artifacts(self, run_id: str):
        return []

    async def get_quarantined_artifacts(self, run_id: str):
        return []

    async def get_pending_resolutions(self, run_id: str):
        return []

    async def pause(self, run_id: str) -> None:
        return None

    async def cancel(self, run_id: str, *, graceful: bool) -> None:
        return None

    async def retry_failed(self, run_id: str, artifact_ids) -> None:
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


def _jira_source() -> SourceConfig:
    return SourceConfig(
        id="src-jira",
        project_id="proj-a",
        source_type="jira",
        name="AuTa board",
        config={
            "base_url": "https://example.atlassian.net",
            "email": "person@example.com",
            "token": {"secret_ref": "secret://fake/1"},
        },
        secret_refs=["secret://fake/1"],
    )


@pytest.mark.blackbox
def test_create_job_returns_202_with_running_job() -> None:
    source = _local_file_source()
    app = create_fastapi_app(ApiSettings())
    service = _service(sources=[source])
    app.dependency_overrides[get_app_service] = lambda: service
    with TestClient(app) as client:
        response = client.post(f"/api/v1/sources/{source.id}/jobs", json={})
        assert response.status_code == 202
        body = response.json()
        assert body["job"]["status"] == "running"
        assert body["job"]["source_id"] == source.id

        job_id = body["job"]["id"]

        status_response = client.get(f"/api/v1/jobs/{job_id}")
        assert status_response.status_code == 200
        assert status_response.json()["job"]["id"] == job_id
        assert status_response.json()["live"]["execution_status"] == "running"

        result_response = client.get(f"/api/v1/jobs/{job_id}/result")
        assert result_response.status_code == 200
        assert result_response.json()["result"] == {"status": "completed"}

        list_response = client.get("/api/v1/jobs", params={"source_id": source.id})
        assert list_response.status_code == 200
        assert [job["id"] for job in list_response.json()] == [job_id]

        action_response = client.post(f"/api/v1/jobs/{job_id}/actions", json={"action": "cancel"})
        assert action_response.status_code == 200
        assert action_response.json()["job"]["status"] == "cancelled"


@pytest.mark.blackbox
def test_create_job_for_a_remote_jira_source_never_leaks_the_secret() -> None:
    """AC2 (67-HARBORRAG.md): a remote connector (jira) drives a job through the
    same path as local_file, and the resolved secret value never appears in any
    job response or in the persisted Job's payload -- only local_file's config
    (a bare path) has no secret to leak, so this is the case that actually
    exercises the "no raw secret in job data" guarantee."""
    source = _jira_source()
    app = create_fastapi_app(ApiSettings())
    service = _service(sources=[source])
    app.dependency_overrides[get_app_service] = lambda: service
    with TestClient(app) as client:
        response = client.post(f"/api/v1/sources/{source.id}/jobs", json={})
        assert response.status_code == 202
        body = response.json()
        assert body["job"]["status"] == "running"
        job_id = body["job"]["id"]

        status_response = client.get(f"/api/v1/jobs/{job_id}")
        result_response = client.get(f"/api/v1/jobs/{job_id}/result")
        list_response = client.get("/api/v1/jobs", params={"source_id": source.id})

        for raw_response in (response, status_response, result_response, list_response):
            assert "secret://fake/1" not in raw_response.text

        job = asyncio.run(service._control_plane().jobs.get(job_id))
        assert job is not None
        assert job.payload == {"connector_name": "jira"}


@pytest.mark.blackbox
def test_create_job_unknown_source_returns_enveloped_404() -> None:
    app = create_fastapi_app(ApiSettings())
    service = _service()
    app.dependency_overrides[get_app_service] = lambda: service
    with TestClient(app) as client:
        response = client.post("/api/v1/sources/does-not-exist/jobs", json={})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "harbor_not_found_error"


@pytest.mark.blackbox
def test_get_unknown_job_returns_enveloped_404() -> None:
    app = create_fastapi_app(ApiSettings())
    service = _service()
    app.dependency_overrides[get_app_service] = lambda: service
    with TestClient(app) as client:
        response = client.get("/api/v1/jobs/does-not-exist")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "harbor_not_found_error"
