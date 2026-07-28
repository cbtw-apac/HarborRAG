"""Read-side project endpoints (ML1/M1) over the mock app service."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control import mock_app_service
from harborrag_core.domain.project import Project


@pytest.mark.blackbox
def test_list_projects_empty_in_mock_mode() -> None:
    """Dev/mock composition has no persisted projects yet."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/api/v1/projects")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.blackbox
def test_list_projects_returns_seeded_project() -> None:
    """Swap in a MockAppService pre-loaded with one project to test the
    read routes end to end without a real control-plane database."""
    app = create_fastapi_app(ApiSettings())
    with TestClient(app) as client:
        app.state.app_service = mock_app_service(
            projects=[
                Project(id="demo-1", name="Demo", collection="demo_collection")
            ]
        )
        response = client.get("/api/v1/projects")
        assert response.status_code == 200
        assert [project["id"] for project in response.json()] == ["demo-1"]

        response = client.get("/api/v1/projects/demo-1")
        assert response.status_code == 200
        assert response.json()["name"] == "Demo"


@pytest.mark.blackbox
def test_get_unknown_project_returns_enveloped_404() -> None:
    """Missing project ids surface through the shared error envelope."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/api/v1/projects/does-not-exist")
        assert response.status_code == 404
        body = response.json()
        assert body["error"]["code"] == "harbor_not_found_error"
        assert "does-not-exist" in body["error"]["message"]
