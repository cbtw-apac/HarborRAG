"""Cross-tenant isolation on the ML1 read routes (regression for the critical
control-plane tenant-scoping gap).

Every route here must show a reader-role principal only the records for
tenants it holds a claim for, and hide (404, not leak) records belonging to
other tenants -- even though all of them live in the same shared repository.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app_test_control_plane import control_plane_app_service
from fastapi.testclient import TestClient

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.dependencies import get_app_service
from harborrag_app.api.settings import ApiSettings
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.job import Job
from harborrag_core.domain.project import Project, ProjectStats
from harborrag_core.domain.source_config import SourceConfig

SECRET = "test-secret-at-least-32-bytes-long-for-hs256"


def _token(tenants: list[str]) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": "u1",
        "role": "reader",
        "tenants": tenants,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "iss": "harborrag",
        "aud": "harborrag-api",
    }
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _client(app) -> TestClient:  # noqa: ANN001 - FastAPI app, kept untyped to avoid a heavy import
    settings_app = app
    return TestClient(settings_app, raise_server_exceptions=False)


def _seeded_app():  # noqa: ANN202 - returns a FastAPI app wired for this module's fixtures
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=SECRET))
    app.dependency_overrides[get_app_service] = lambda: control_plane_app_service(
        projects=[
            Project(
                id="proj-a",
                tenant_id="tenant-a",
                name="A",
                collection="a",
                stats=ProjectStats(documents=3, chunks=9),
            ),
            Project(
                id="proj-b",
                tenant_id="tenant-b",
                name="B",
                collection="b",
                stats=ProjectStats(documents=100, chunks=500),
            ),
        ],
        sources=[
            SourceConfig(
                id="src-a",
                tenant_id="tenant-a",
                project_id="proj-a",
                source_type="local_file",
                name="Source A",
            ),
            SourceConfig(
                id="src-b",
                tenant_id="tenant-b",
                project_id="proj-b",
                source_type="local_file",
                name="Source B",
            ),
        ],
        jobs=[
            Job(
                id="job-a1",
                tenant_id="tenant-a",
                source_id="src-a",
                project_id="proj-a",
                job_type="bulk_ingest",
                status="queued",
            ),
            Job(
                id="job-a2",
                tenant_id="tenant-a",
                source_id="src-a",
                project_id="proj-a",
                job_type="bulk_ingest",
                status="running",
            ),
            Job(
                id="job-b1",
                tenant_id="tenant-b",
                source_id="src-b",
                project_id="proj-b",
                job_type="bulk_ingest",
                status="failed",
            ),
        ],
        activity=[
            ActivityEntry(
                id="act-a",
                tenant_id="tenant-a",
                actor="alice",
                verb="created",
                entity_type="project",
                entity_id="proj-a",
                summary="alice created proj-a",
            ),
            ActivityEntry(
                id="act-b",
                tenant_id="tenant-b",
                actor="bob",
                verb="created",
                entity_type="project",
                entity_id="proj-b",
                summary="bob created proj-b",
            ),
        ],
    )
    return app


@pytest.mark.blackbox
def test_list_projects_hides_other_tenants_rows() -> None:
    with _client(_seeded_app()) as client:
        response = client.get(
            "/api/v1/projects", headers={"Authorization": f"Bearer {_token(['tenant-a'])}"}
        )
        assert response.status_code == 200
        assert [p["id"] for p in response.json()] == ["proj-a"]


@pytest.mark.blackbox
def test_get_project_404s_for_other_tenants_row() -> None:
    with _client(_seeded_app()) as client:
        response = client.get(
            "/api/v1/projects/proj-b", headers={"Authorization": f"Bearer {_token(['tenant-a'])}"}
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "harbor_not_found_error"

        own = client.get(
            "/api/v1/projects/proj-a", headers={"Authorization": f"Bearer {_token(['tenant-a'])}"}
        )
        assert own.status_code == 200


@pytest.mark.blackbox
def test_list_sources_hides_other_tenants_rows() -> None:
    with _client(_seeded_app()) as client:
        response = client.get(
            "/api/v1/sources", headers={"Authorization": f"Bearer {_token(['tenant-a'])}"}
        )
        assert response.status_code == 200
        assert [s["id"] for s in response.json()] == ["src-a"]


@pytest.mark.blackbox
def test_get_source_404s_for_other_tenants_row() -> None:
    with _client(_seeded_app()) as client:
        response = client.get(
            "/api/v1/sources/src-b", headers={"Authorization": f"Bearer {_token(['tenant-a'])}"}
        )
        assert response.status_code == 404


@pytest.mark.blackbox
def test_list_activity_hides_other_tenants_rows() -> None:
    with _client(_seeded_app()) as client:
        response = client.get(
            "/api/v1/activity", headers={"Authorization": f"Bearer {_token(['tenant-a'])}"}
        )
        assert response.status_code == 200
        assert [e["id"] for e in response.json()] == ["act-a"]


@pytest.mark.blackbox
def test_metrics_excludes_other_tenants_rows() -> None:
    with _client(_seeded_app()) as client:
        response = client.get(
            "/api/v1/metrics/ingestion", headers={"Authorization": f"Bearer {_token(['tenant-a'])}"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["projects_total"] == 1
        assert body["sources_total"] == 1
        assert body["documents_total"] == 3  # proj-a only, not proj-b's 100
        assert body["chunks_total"] == 9  # proj-a only, not proj-b's 500
        assert body["jobs_by_status"] == {
            "queued": 1,
            "running": 1,
            "succeeded": 0,
            "failed": 0,  # job-b1 (tenant-b) must not be counted here
            "cancelled": 0,
        }


@pytest.mark.blackbox
def test_multi_tenant_principal_sees_the_union_of_its_tenants() -> None:
    with _client(_seeded_app()) as client:
        response = client.get(
            "/api/v1/projects",
            headers={"Authorization": f"Bearer {_token(['tenant-a', 'tenant-b'])}"},
        )
        assert response.status_code == 200
        assert {p["id"] for p in response.json()} == {"proj-a", "proj-b"}


@pytest.mark.blackbox
def test_wildcard_dev_principal_still_sees_every_tenant() -> None:
    """auth_mode=none (the dev default) must keep its unrestricted view."""
    app = create_fastapi_app(ApiSettings())
    app.dependency_overrides[get_app_service] = lambda: control_plane_app_service(
        projects=[
            Project(id="proj-a", tenant_id="tenant-a", name="A", collection="a"),
            Project(id="proj-b", tenant_id="tenant-b", name="B", collection="b"),
        ],
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/projects")
        assert response.status_code == 200
        assert {p["id"] for p in response.json()} == {"proj-a", "proj-b"}
