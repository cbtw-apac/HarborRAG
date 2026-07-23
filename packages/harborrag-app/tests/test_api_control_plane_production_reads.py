"""ML1 read routes over a real (temp SQLite) control-plane DB.

test_api_projects/sources/activity/settings/metrics.py exercise MockAppService
only; this file drives the same endpoints through AppService (production)
so the SQL-backed code paths in app_service.py are actually covered, not
just verified by hand.

Seeding goes through CompositionRoot.production() (harborrag_runtime), never
through harborrag_adapters directly — harborrag-app may depend on
harborrag-runtime but not on harborrag-adapters (deps-check enforces this
for tests too).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.project import Project, ProjectStats
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.domain.source_config import SourceConfig
from harborrag_runtime.composition import CompositionRoot
from harborrag_runtime.settings import RuntimeSettings


async def _seed(dsn: str) -> None:
    composition = CompositionRoot.production(RuntimeSettings(control_db_url=dsn))
    control_plane = composition.control_plane
    assert control_plane is not None
    await control_plane.projects.create(
        Project(
            id="proj-1",
            name="Demo",
            collection="demo_collection",
            stats=ProjectStats(documents=3, chunks=9),
        )
    )
    await control_plane.sources.create(
        SourceConfig(
            id="src-1",
            project_id="proj-1",
            source_type="local_file",
            name="Docs",
            config={"path": "./docs"},
        )
    )
    await control_plane.activity.append(
        ActivityEntry(
            id="a1",
            actor="alice",
            verb="created",
            entity_type="source",
            entity_id="src-1",
            summary="alice created src-1",
        )
    )
    await control_plane.settings.put(WorkspaceSettings(data={"theme": "dark"}))


@pytest.fixture
def seeded_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    asyncio.run(_seed(dsn))
    monkeypatch.setenv("HARBORRAG_ENV", "dev")
    monkeypatch.setenv("HARBORRAG_CONTROL_DB_URL", dsn)
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        yield client


@pytest.mark.blackbox
def test_projects_and_sources_read_from_real_db(seeded_client) -> None:
    projects = seeded_client.get("/api/v1/projects").json()
    assert [p["id"] for p in projects] == ["proj-1"]

    project = seeded_client.get("/api/v1/projects/proj-1").json()
    assert project["stats"]["documents"] == 3

    assert seeded_client.get("/api/v1/projects/does-not-exist").status_code == 404

    sources = seeded_client.get("/api/v1/sources", params={"project_id": "proj-1"}).json()
    assert [s["id"] for s in sources] == ["src-1"]
    assert seeded_client.get("/api/v1/sources/does-not-exist").status_code == 404


@pytest.mark.blackbox
def test_activity_settings_and_metrics_read_from_real_db(seeded_client) -> None:
    activity = seeded_client.get("/api/v1/activity").json()
    assert [entry["id"] for entry in activity] == ["a1"]

    settings = seeded_client.get("/api/v1/settings").json()
    assert settings == {"data": {"theme": "dark"}}

    metrics = seeded_client.get("/api/v1/metrics").json()
    assert metrics["projects_total"] == 1
    assert metrics["sources_total"] == 1
    assert metrics["documents_total"] == 3
    assert metrics["chunks_total"] == 9
    assert metrics["jobs_by_status"] == {
        "queued": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
    }
