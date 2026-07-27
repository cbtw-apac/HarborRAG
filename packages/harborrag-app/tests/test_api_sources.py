"""Read-side source endpoints (ML1/M1) over the mock app service."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.services.mock import MockAppService
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
    with TestClient(app) as client:
        app.state.app_service = MockAppService(
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
