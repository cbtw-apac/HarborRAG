"""ST8 selection rule: lightweight development composition or production."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.services.app_service import AppService
from harborrag_app.services.selection import select_app_service


@pytest.mark.blackbox
def test_dev_without_control_db_selects_development_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare development uses the real app service without database provisioning."""
    monkeypatch.setenv("HARBORRAG_ENV", "dev")
    monkeypatch.delenv("HARBORRAG_CONTROL_DB_URL", raising=False)
    service, mode = select_app_service()
    assert isinstance(service, AppService)
    assert mode == "development"
    assert service.health().data["diagnostics"]["mode"] == "development"


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
    """Factory + lifespan against a real control DB: readyz 200 and
    diagnostics reports composition_mode=production."""
    monkeypatch.setenv("HARBORRAG_ENV", "dev")
    monkeypatch.setenv("HARBORRAG_CONTROL_DB_URL", f"sqlite+aiosqlite:///{tmp_path}/control.db")
    with TestClient(create_fastapi_app(ApiSettings())) as client:
        ready = client.get("/api/v1/readyz")
        assert ready.status_code == 200
        diagnostics = client.get("/api/v1/diagnostics")
        assert diagnostics.status_code == 200
        payload = diagnostics.json()
        assert payload["composition_mode"] == "production"
        assert payload["diagnostics"]["diagnostics"]["mode"] == "production"
