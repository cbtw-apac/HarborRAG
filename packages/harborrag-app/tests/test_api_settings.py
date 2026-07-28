"""Read-side workspace settings endpoint (ML1/M1) over the mock app service."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control import mock_app_service
from harborrag_core.domain.settings import WorkspaceSettings


@pytest.mark.blackbox
def test_get_settings_empty_document_by_default() -> None:
    """A workspace that never called put() gets an empty document, not 404."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/api/v1/settings")
        assert response.status_code == 200
        assert response.json() == {"data": {}}


@pytest.mark.blackbox
def test_get_settings_returns_seeded_document() -> None:
    """Seeded settings pass through untouched."""
    app = create_fastapi_app(ApiSettings())
    with TestClient(app) as client:
        app.state.app_service = mock_app_service(
            settings=WorkspaceSettings(data={"theme": "dark"})
        )
        response = client.get("/api/v1/settings")
        assert response.status_code == 200
        assert response.json() == {"data": {"theme": "dark"}}
