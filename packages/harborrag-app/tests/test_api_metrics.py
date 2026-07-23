"""Read-side dashboard metrics endpoint (ML1/M1) over the mock app service."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.services.mock import MockAppService
from harborrag_core.domain.project import Project, ProjectStats
from harborrag_core.domain.source_config import SourceConfig


@pytest.mark.blackbox
def test_metrics_all_zero_on_a_fresh_workspace() -> None:
    """A brand-new workspace must not crash the metrics aggregation."""
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        response = client.get("/api/v1/metrics")
        assert response.status_code == 200
        assert response.json() == {
            "projects_total": 0,
            "sources_total": 0,
            "documents_total": 0,
            "chunks_total": 0,
            "jobs_by_status": {
                "queued": 0,
                "running": 0,
                "succeeded": 0,
                "failed": 0,
                "cancelled": 0,
            },
        }


@pytest.mark.blackbox
def test_metrics_reflects_seeded_projects_and_sources() -> None:
    """Counters aggregate across every seeded project/source."""
    app = create_fastapi_app(ApiSettings())
    with TestClient(app) as client:
        app.state.app_service = MockAppService(
            projects=[
                Project(
                    id="proj-1",
                    name="Docs",
                    collection="docs_collection",
                    stats=ProjectStats(documents=10, chunks=120),
                ),
                Project(
                    id="proj-2",
                    name="Jira",
                    collection="jira_collection",
                    stats=ProjectStats(documents=5, chunks=30),
                ),
            ],
            sources=[
                SourceConfig(
                    id="src-1", project_id="proj-1", source_type="local_file", name="Docs"
                )
            ],
        )
        response = client.get("/api/v1/metrics")
        assert response.status_code == 200
        body = response.json()
        assert body["projects_total"] == 2
        assert body["sources_total"] == 1
        assert body["documents_total"] == 15
        assert body["chunks_total"] == 150
