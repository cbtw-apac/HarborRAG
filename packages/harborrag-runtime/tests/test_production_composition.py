"""CompositionRoot.production boots against a real (SQLite) control DB (ST8)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_core.domain.project import Project
from harborrag_runtime.composition import CompositionRoot
from harborrag_runtime.config.settings import DEFAULT_CONTROL_DB_URL, RuntimeSettings


def _production(tmp_path: Path) -> CompositionRoot:
    """Composition against a tmp SQLite control DB."""
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    return CompositionRoot.production(RuntimeSettings(control_db_url=dsn))


@pytest.mark.whitebox
def test_production_composition_migrates_and_reports_ready(tmp_path: Path, caplog) -> None:
    """production() runs migrations, probes the DB, and reports ready
    diagnostics with the stamped migration version."""
    with caplog.at_level(logging.INFO, logger="harborrag.runtime.composition"):
        composition = _production(tmp_path)
    assert composition.mode == "production"
    diagnostics = composition.diagnostics()
    assert diagnostics["mode"] == "production"
    runtime = diagnostics["runtime"]
    assert isinstance(runtime, dict)
    assert runtime["ready"] is True
    control_db = runtime["control_db"]
    assert control_db["ping"] == "ok"
    assert control_db["migrations"] == "0013"
    assert control_db["scheme"] == "sqlite+aiosqlite"
    assert "Control-plane composition completed" in caplog.text
    assert "database_scheme=sqlite+aiosqlite ready=True migration=0013" in caplog.text


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_production_repositories_hit_the_real_database(
    tmp_path: Path,
) -> None:
    """The composed port-typed repositories read/write the migrated DB."""
    composition = _production(tmp_path)
    try:
        assert composition.control_plane is not None
        projects = composition.control_plane.projects
        await projects.create(
            Project(id="p1", tenant_id="DEFAULT", name="Docs", collection="docs_main")
        )
        fetched = await projects.get("p1", tenant_ids=None)
        assert fetched is not None and fetched.name == "Docs"
    finally:
        await composition.aclose()


@pytest.mark.whitebox
def test_production_migration_failure_aborts_startup(tmp_path: Path) -> None:
    """An unreachable control DB must not leave a degraded process serving traffic."""

    settings = RuntimeSettings(control_db_url=f"sqlite+aiosqlite:///{tmp_path}/nodir/x.db")
    with pytest.raises(HarborConfigurationError, match="migrations failed"):
        CompositionRoot.production(settings)


@pytest.mark.whitebox
def test_production_probe_failure_aborts_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "harborrag_runtime.composition.control_plane._probe_control_db",
        lambda _dsn: {"ping": "failed", "error": "probe failed", "scheme": "sqlite"},
    )

    with pytest.raises(HarborConfigurationError, match="database probe failed"):
        _production(tmp_path)


@pytest.mark.whitebox
def test_prod_env_refuses_default_control_db_url() -> None:
    """Fail closed: env=prod with the default sqlite control_db_url must not
    boot, mirroring the auth_mode=none-in-prod guard. control_db_url is
    passed explicitly so the test exercises the intended default regardless
    of an ambient HARBORRAG_CONTROL_DB_URL in the environment."""
    with pytest.raises(ValueError, match="SQLite is development-only"):
        RuntimeSettings(env="prod", control_db_url=DEFAULT_CONTROL_DB_URL)


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_prod_env_rejects_every_explicit_sqlite_url(tmp_path: Path) -> None:
    """An alternate filename must not bypass the production database policy."""
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    with pytest.raises(ValueError, match="SQLite is development-only"):
        RuntimeSettings(env="prod", control_db_url=dsn)


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_dev_env_allows_default_control_db_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env=dev keeps booting against the default sqlite control_db_url.
    control_db_url is passed explicitly so the test deterministically
    exercises the intended default regardless of the environment."""
    monkeypatch.chdir(tmp_path)
    composition = CompositionRoot.production(
        RuntimeSettings(env="dev", control_db_url=DEFAULT_CONTROL_DB_URL)
    )
    try:
        assert composition.mode == "production"
        assert composition.control_plane is not None
    finally:
        await composition.aclose()


@pytest.mark.whitebox
def test_migration_failure_logs_safe_actionable_diagnostics(
    tmp_path: Path,
    caplog,
) -> None:
    """Failed startup logs a safe cause and an actionable migration hint.

    A schema built without Alembic recording it makes the runner replay from base and
    collide with existing tables. Logging only ``error_type=OperationalError`` leaves no
    way to tell that apart from a connection failure, and the real symptom surfaces much
    later as a missing column in an unrelated query.
    """

    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    # Build the schema without stamping it, exactly as an un-migrated database looks.
    import sqlite3

    connection = sqlite3.connect(f"{tmp_path}/control.db")
    connection.execute("CREATE TABLE projects (id TEXT NOT NULL, PRIMARY KEY (id))")
    connection.commit()
    connection.close()

    with caplog.at_level(logging.ERROR, logger="harborrag.runtime.composition"):
        with pytest.raises(HarborConfigurationError, match="migrations failed"):
            CompositionRoot.production(RuntimeSettings(control_db_url=dsn))

    message = caplog.text
    assert "Control-plane migrations failed" in message
    assert "error_type=OperationalError" in message
    assert "already exists" not in message
    assert "hint=" in message, "the recoverable case must name its remedy"
