"""Read-side activity (audit feed) endpoint (ML1/M1) over the mock app service."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control import mock_app_service
from harborrag_core.domain.activity import ActivityEntry


@pytest.mark.blackbox
def test_list_activity_empty_in_mock_mode() -> None:
    """Dev/mock composition has no audit entries yet."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/api/v1/activity")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.blackbox
def test_list_activity_returns_seeded_entries_newest_first() -> None:
    """Seeded entries come back newest first, respecting the limit param."""
    app = create_fastapi_app(ApiSettings())
    with TestClient(app) as client:
        app.state.app_service = mock_app_service(
            activity=[
                ActivityEntry(
                    id="a1",
                    actor="alice",
                    verb="created",
                    entity_type="source",
                    entity_id="src-1",
                    summary="alice created source src-1",
                ),
                ActivityEntry(
                    id="a2",
                    actor="bob",
                    verb="updated",
                    entity_type="project",
                    entity_id="proj-1",
                    summary="bob updated project proj-1",
                ),
            ]
        )

        response = client.get("/api/v1/activity")
        assert response.status_code == 200
        assert [entry["id"] for entry in response.json()] == ["a2", "a1"]

        response = client.get("/api/v1/activity", params={"limit": 1})
        assert response.status_code == 200
        assert [entry["id"] for entry in response.json()] == ["a2"]
