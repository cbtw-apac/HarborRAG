"""Source endpoints over the mock app service: read side (ML1/M1) and write
side + catalog (ML2)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.dependencies import get_app_service
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control import mock_app_service
from harborrag_core.domain.project import Project
from harborrag_core.domain.source_config import SourceConfig


@pytest.mark.blackbox
def test_list_sources_empty_in_mock_mode() -> None:
    """Dev/mock composition has no persisted sources yet."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/api/v1/sources")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.blackbox
def test_list_sources_filters_by_project_and_hides_secrets() -> None:
    """Seeded sources scope by project_id; only secret_refs ever appear.

    proj-b's config carries a stray "token" value (a bug/migration edge, per
    the SourceConfig docstring config should only ever hold secret_ref
    placeholders) to prove the DTO boundary masks it rather than merely
    happening not to contain one.
    """
    app = create_fastapi_app(ApiSettings())
    app.dependency_overrides[get_app_service] = lambda: mock_app_service(
        sources=[
            SourceConfig(
                id="src-1",
                project_id="proj-a",
                source_type="local_file",
                name="Docs folder",
                config={"path": "./docs"},
                secret_refs=[],
            ),
            SourceConfig(
                id="src-2",
                project_id="proj-b",
                source_type="jira",
                name="AuTa board",
                config={
                    "base_url": "https://example.atlassian.net",
                    "token": "hunter2",
                },
                secret_refs=["secret://fake/1"],
            ),
        ]
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/sources", params={"project_id": "proj-b"})
        assert response.status_code == 200
        [source] = response.json()
        assert source["id"] == "src-2"
        assert source["secret_refs"] == ["secret://fake/1"]
        assert source["config"]["token"] == "<redacted>"
        assert "hunter2" not in source["config"].values()

        response = client.get("/api/v1/sources/src-1")
        assert response.status_code == 200
        assert response.json()["name"] == "Docs folder"


@pytest.mark.blackbox
def test_get_unknown_source_returns_enveloped_404() -> None:
    """Missing source ids surface through the shared error envelope."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/api/v1/sources/does-not-exist")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "harbor_not_found_error"


@pytest.mark.blackbox
def test_source_types_catalog_lists_secret_fields() -> None:
    """The wizard catalog exposes which config fields are secret-shaped."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/api/v1/source-types")
        assert response.status_code == 200
        by_type = {entry["source_type"]: entry for entry in response.json()}
        assert "token" in by_type["jira"]["secret_fields"]
        assert "base_url" in by_type["jira"]["fields"]


@pytest.mark.blackbox
def test_create_source_never_echoes_the_raw_secret_value() -> None:
    """POST /sources extracts secret fields; the response only ever shows a ref."""
    app = create_fastapi_app(ApiSettings())
    # One service instance shared across requests -- a fresh mock_app_service()
    # per call would reset its in-memory activity/source fakes every request.
    service = mock_app_service(projects=[Project(id="proj-a", name="A", collection="a")])
    app.dependency_overrides[get_app_service] = lambda: service
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/sources",
            json={
                "project_id": "proj-a",
                "source_type": "jira",
                "name": "Support board",
                "config": {
                    "base_url": "https://example.atlassian.net",
                    "email": "person@example.com",
                    "token": "hunter2",
                },
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["config"]["token"] == "<redacted>"
        assert "hunter2" not in response.text
        assert body["secret_refs"]

        [entry] = client.get("/api/v1/activity").json()
        assert entry["verb"] == "created"
        assert "hunter2" not in entry["summary"]


@pytest.mark.blackbox
def test_create_source_unknown_project_returns_enveloped_404() -> None:
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.post(
            "/api/v1/sources",
            json={
                "project_id": "ghost",
                "source_type": "local_file",
                "name": "docs",
                "config": {"path": "./docs"},
            },
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "harbor_not_found_error"


@pytest.mark.blackbox
def test_create_source_unsupported_type_returns_501() -> None:
    app = create_fastapi_app(ApiSettings())
    app.dependency_overrides[get_app_service] = lambda: mock_app_service(
        projects=[Project(id="proj-a", name="A", collection="a")]
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/sources",
            json={
                "project_id": "proj-a",
                "source_type": "notion",
                "name": "docs",
                "config": {},
            },
        )
        assert response.status_code == 501
        assert response.json()["error"]["code"] == "harbor_capability_error"


@pytest.mark.blackbox
def test_update_source_omits_untouched_fields() -> None:
    app = create_fastapi_app(ApiSettings())
    app.dependency_overrides[get_app_service] = lambda: mock_app_service(
        projects=[Project(id="proj-a", name="A", collection="a")],
        sources=[
            SourceConfig(
                id="src-1",
                project_id="proj-a",
                source_type="local_file",
                name="Docs folder",
                config={"path": "./docs"},
            )
        ],
    )
    with TestClient(app) as client:
        response = client.patch("/api/v1/sources/src-1", json={"name": "Renamed"})
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Renamed"
        assert body["config"] == {"path": "./docs"}


@pytest.mark.blackbox
def test_delete_source_returns_204_then_404_on_get() -> None:
    app = create_fastapi_app(ApiSettings())
    service = mock_app_service(
        projects=[Project(id="proj-a", name="A", collection="a")],
        sources=[
            SourceConfig(
                id="src-1",
                project_id="proj-a",
                source_type="local_file",
                name="Docs folder",
                config={"path": "./docs"},
            )
        ],
    )
    app.dependency_overrides[get_app_service] = lambda: service
    with TestClient(app) as client:
        response = client.delete("/api/v1/sources/src-1")
        assert response.status_code == 204
        assert client.get("/api/v1/sources/src-1").status_code == 404
