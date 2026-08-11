"""Write-side source endpoints (ML2): create/update/delete + secrets handling."""

from __future__ import annotations

import pytest
from app_test_control_plane import control_plane_app_service
from fastapi.testclient import TestClient

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.dependencies import get_principal
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.dependencies import get_app_service
from harborrag_app.api.settings import ApiSettings
from harborrag_core.domain.project import Project
from harborrag_core.testing.fakes import FakeSecrets


def _client(**service_kwargs: object) -> TestClient:
    """One AppService instance shared by every request the client makes.

    dependency_overrides is resolved fresh per request -- returning a new
    control_plane_app_service() from the override itself would silently
    reset all in-memory state (sources, secrets) between requests.
    """
    app = create_fastapi_app(ApiSettings())
    service = control_plane_app_service(**service_kwargs)
    app.dependency_overrides[get_app_service] = lambda: service
    return TestClient(app)


@pytest.fixture
def project() -> Project:
    return Project(id="proj-a", name="A", collection="a", tenant_id="DEFAULT")


@pytest.mark.blackbox
def test_create_source_never_leaks_the_secret_it_was_given(project: Project) -> None:
    """The real secret VALUE (not just its ref) must never appear anywhere in the response."""
    sentinel = "sentinel-raw-token-should-never-leak"
    with _client(projects=[project]) as client:
        response = client.post(
            "/api/v1/sources",
            json={
                "tenant_id": "DEFAULT",
                "project_id": "proj-a",
                "source_type": "jira",
                "name": "AuTa board",
                "config": {"base_url": "https://example.atlassian.net", "token": sentinel},
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert sentinel not in response.text
        assert body["config"]["token"] == "<redacted>"
        assert body["secret_refs"] and body["secret_refs"][0].startswith("secret://fake/")

        # Confirm it round-trips the same way on read.
        get_response = client.get(f"/api/v1/sources/{body['id']}")
        assert get_response.status_code == 200
        assert sentinel not in get_response.text


@pytest.mark.blackbox
def test_create_source_rejects_unknown_project(project: Project) -> None:
    with _client(projects=[project]) as client:
        response = client.post(
            "/api/v1/sources",
            json={
                "tenant_id": "DEFAULT",
                "project_id": "does-not-exist",
                "source_type": "local",
                "name": "Docs",
                "config": {"source_path": "./docs"},
            },
        )
        assert response.status_code == 404


@pytest.mark.blackbox
def test_update_source_rejects_explicit_null_config(project: Project) -> None:
    """{"config": null} must 422, not crash inside secrets extraction."""
    with _client(projects=[project]) as client:
        created = client.post(
            "/api/v1/sources",
            json={
                "tenant_id": "DEFAULT",
                "project_id": "proj-a",
                "source_type": "local",
                "name": "Docs",
                "config": {"source_path": "./docs"},
            },
        ).json()

        response = client.patch(f"/api/v1/sources/{created['id']}", json={"config": None})
        assert response.status_code == 422


@pytest.mark.blackbox
def test_update_source_swaps_secret_and_retires_the_old_ref(project: Project) -> None:
    secrets = FakeSecrets()
    with _client(projects=[project], secrets=secrets) as client:
        created = client.post(
            "/api/v1/sources",
            json={
                "tenant_id": "DEFAULT",
                "project_id": "proj-a",
                "source_type": "jira",
                "name": "AuTa board",
                "config": {"base_url": "https://example.atlassian.net", "token": "old-token"},
            },
        ).json()
        old_ref = created["secret_refs"][0]
        assert secrets.values[old_ref] == "old-token"

        updated = client.patch(
            f"/api/v1/sources/{created['id']}",
            json={"config": {"base_url": "https://example.atlassian.net", "token": "new-token"}},
        ).json()
        new_ref = updated["secret_refs"][0]

        assert new_ref != old_ref
        assert secrets.values[new_ref] == "new-token"
        assert old_ref not in secrets.values


@pytest.mark.blackbox
def test_delete_source_forgets_every_secret_it_referenced(project: Project) -> None:
    secrets = FakeSecrets()
    with _client(projects=[project], secrets=secrets) as client:
        created = client.post(
            "/api/v1/sources",
            json={
                "tenant_id": "DEFAULT",
                "project_id": "proj-a",
                "source_type": "jira",
                "name": "AuTa board",
                "config": {"base_url": "https://example.atlassian.net", "token": "hunter2"},
            },
        ).json()
        ref = created["secret_refs"][0]
        assert ref in secrets.values

        response = client.delete(f"/api/v1/sources/{created['id']}")
        assert response.status_code == 204
        assert ref not in secrets.values
        assert client.get(f"/api/v1/sources/{created['id']}").status_code == 404


@pytest.mark.blackbox
def test_source_outside_the_callers_tenants_404s_not_403s(project: Project) -> None:
    """Existence of a source in another tenant must not leak via a 403."""
    app = create_fastapi_app(ApiSettings())
    service = control_plane_app_service(projects=[project])
    app.dependency_overrides[get_app_service] = lambda: service
    with TestClient(app) as client:
        create_response = client.post(
            "/api/v1/sources",
            json={
                "tenant_id": "DEFAULT",
                "project_id": "proj-a",
                "source_type": "local",
                "name": "Docs",
                "config": {"source_path": "./docs"},
            },
        )
        assert create_response.status_code == 201
        created = create_response.json()

        # Sanity: the default (owner, tenant "*") principal really can see it.
        assert client.get(f"/api/v1/sources/{created['id']}").status_code == 200
        assert len(client.get("/api/v1/sources").json()) == 1

        app.dependency_overrides[get_principal] = lambda: Principal(
            subject="other-tenant-user",
            role="owner",
            tenant_ids=frozenset({"some-other-tenant"}),
        )
        response = client.get(f"/api/v1/sources/{created['id']}")
        assert response.status_code == 404

        list_response = client.get("/api/v1/sources")
        assert list_response.json() == []
