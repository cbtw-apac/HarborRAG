"""Application-service selection uses the configured durable composition."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.composition.selection import select_app_service
from harborrag_app.workflow_control.composition.service import AppService


@pytest.mark.blackbox
def test_dev_without_explicit_control_db_uses_default_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Bare development provisions the default durable SQLite control plane."""
    monkeypatch.setenv("HARBORRAG_ENV", "dev")
    monkeypatch.delenv("HARBORRAG_CONTROL_DB_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    service, mode = select_app_service()
    assert isinstance(service, AppService)
    assert mode == "production"
    diagnostics = service.health().data["diagnostics"]
    assert diagnostics["mode"] == "production"
    assert diagnostics["runtime"]["control_db"]["scheme"] == "sqlite+aiosqlite"


@pytest.mark.blackbox
def test_control_db_url_selects_production(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A configured HARBORRAG_CONTROL_DB_URL flips to production composition,
    which migrates the DB and reports healthy."""
    monkeypatch.setenv("HARBORRAG_ENV", "dev")
    monkeypatch.setenv("HARBORRAG_CONTROL_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/control.db")
    service, mode = select_app_service()
    assert isinstance(service, AppService)
    assert mode == "production"
    health = service.health()
    assert health.ok is True


@pytest.mark.blackbox
def test_api_boots_production_composition_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Factory lifespan composes production while operational routes stay small."""
    monkeypatch.setenv("HARBORRAG_ENV", "dev")
    monkeypatch.setenv("HARBORRAG_CONTROL_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/control.db")
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/metrics").status_code == 404
        assert client.get("/api/v1/metrics").status_code == 200
        assert client.app.state.composition_mode == "production"
        assert client.app.state.app_service.health().ok is True
