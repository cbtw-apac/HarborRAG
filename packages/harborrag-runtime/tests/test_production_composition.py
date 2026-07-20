"""CompositionRoot.production boots against a real (SQLite) control DB (ST8)."""

from __future__ import annotations

from pathlib import Path

import pytest
from harborrag_core.domain.project import Project
from harborrag_runtime.composition import CompositionRoot
from harborrag_runtime.settings import RuntimeSettings


def _production(tmp_path: Path) -> CompositionRoot:
    """Composition against a tmp SQLite control DB."""
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    return CompositionRoot.production(RuntimeSettings(control_db_url=dsn))


@pytest.mark.whitebox
def test_production_composition_migrates_and_reports_ready(tmp_path: Path) -> None:
    """production() runs migrations, probes the DB, and reports ready
    diagnostics with the stamped migration version."""
    composition = _production(tmp_path)
    assert composition.mode == "production"
    diagnostics = composition.diagnostics()
    assert diagnostics["mode"] == "production"
    runtime = diagnostics["runtime"]
    assert isinstance(runtime, dict)
    assert runtime["ready"] is True
    control_db = runtime["control_db"]
    assert control_db["ping"] == "ok"
    assert control_db["migrations"] == "0001"
    assert control_db["scheme"] == "sqlite+aiosqlite"


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_production_repositories_hit_the_real_database(
    tmp_path: Path,
) -> None:
    """The composed port-typed repositories read/write the migrated DB."""
    composition = _production(tmp_path)
    assert composition.control_plane is not None
    projects = composition.control_plane.projects
    await projects.create(Project(id="p1", name="Docs", collection="docs_main"))
    fetched = await projects.get("p1")
    assert fetched is not None and fetched.name == "Docs"


@pytest.mark.whitebox
def test_production_probe_reports_failure_without_raising(tmp_path: Path) -> None:
    """An unreachable control DB degrades diagnostics instead of crashing."""
    bad = CompositionRoot.production(
        RuntimeSettings(control_db_url=f"sqlite+aiosqlite:///{tmp_path}/nodir/x.db")
    )
    runtime = bad.runtime_service.diagnostics()
    assert runtime["ready"] is False


@pytest.mark.whitebox
def test_local_composition_stays_mock() -> None:
    """local() keeps the deterministic mock wiring and 'local' mode."""
    composition = CompositionRoot.local()
    assert composition.mode == "local"
    assert composition.control_plane is None
    assert composition.diagnostics()["runtime"] == {
        "provider": "mock_runtime",
        "ready": True,
    }
