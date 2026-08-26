"""Read-side activity endpoint over the development app service."""

from __future__ import annotations

import pytest
from app_test_control_plane import control_plane_app_service
from fastapi.testclient import TestClient

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.dependencies import get_app_service
from harborrag_app.api.settings import ApiSettings
from harborrag_core.domain.activity import ActivityEntry


@pytest.mark.blackbox
def test_list_activity_empty_in_development_mode() -> None:
    """Development composition has no audit entries yet."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/api/v1/activity")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.blackbox
def test_list_activity_returns_seeded_entries_newest_first() -> None:
    """Seeded entries come back newest first, respecting the limit param."""
    app = create_fastapi_app(ApiSettings())
    app.dependency_overrides[get_app_service] = lambda: control_plane_app_service(
        activity=[
            ActivityEntry(
                id="a1",
                tenant_id="DEFAULT",
                actor="alice",
                verb="created",
                entity_type="source",
                entity_id="src-1",
                summary="alice created source src-1",
            ),
            ActivityEntry(
                id="a2",
                tenant_id="DEFAULT",
                actor="bob",
                verb="updated",
                entity_type="project",
                entity_id="proj-1",
                summary="bob updated project proj-1",
            ),
        ]
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/activity")
        assert response.status_code == 200
        assert [entry["id"] for entry in response.json()] == ["a2", "a1"]

        response = client.get("/api/v1/activity", params={"limit": 1})
        assert response.status_code == 200
        assert [entry["id"] for entry in response.json()] == ["a2"]
