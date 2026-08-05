"""CompositionRoot.production boots against a real (SQLite) control DB (ST8)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

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
    assert control_db["migrations"] == "0009"
    assert control_db["scheme"] == "sqlite+aiosqlite"
    assert "Control-plane composition completed" in caplog.text
    assert "database_scheme=sqlite+aiosqlite ready=True migration=0009" in caplog.text


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
        await projects.create(Project(id="p1", name="Docs", collection="docs_main"))
        fetched = await projects.get("p1")
        assert fetched is not None and fetched.name == "Docs"
    finally:
        await composition.aclose()


@pytest.mark.whitebox
def test_production_probe_reports_failure_without_raising(tmp_path: Path) -> None:
    """An unreachable control DB degrades diagnostics instead of crashing."""
    bad = CompositionRoot.production(
        RuntimeSettings(control_db_url=f"sqlite+aiosqlite:///{tmp_path}/nodir/x.db")
    )
    runtime = bad.diagnostics()["runtime"]
    assert isinstance(runtime, dict)
    assert runtime["ready"] is False


@pytest.mark.whitebox
def test_prod_env_refuses_default_control_db_url() -> None:
    """Fail closed: env=prod with the default sqlite control_db_url must not
    boot, mirroring the auth_mode=none-in-prod guard. control_db_url is
    passed explicitly so the test exercises the intended default regardless
    of an ambient HARBORRAG_CONTROL_DB_URL in the environment."""
    from harborrag_core.contracts.errors import HarborConfigurationError

    with pytest.raises(HarborConfigurationError):
        CompositionRoot.production(
            RuntimeSettings(env="prod", control_db_url=DEFAULT_CONTROL_DB_URL)
        )


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_prod_env_boots_with_explicit_control_db_url(tmp_path: Path) -> None:
    """env=prod with a non-default control_db_url composes normally."""
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    composition = CompositionRoot.production(RuntimeSettings(env="prod", control_db_url=dsn))
    try:
        assert composition.mode == "production"
        assert composition.control_plane is not None
    finally:
        await composition.aclose()


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
def test_migration_failure_logs_the_cause_not_just_the_exception_type(
    tmp_path: Path,
    caplog,
) -> None:
    """Boot degrades silently, so this log is the only statement of the cause.

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
        composition = CompositionRoot.production(RuntimeSettings(control_db_url=dsn))

    message = caplog.text
    assert "Control-plane migrations failed" in message
    assert "already exists" in message, "the cause must reach the log, not just its type"
    assert "hint=" in message, "the recoverable case must name its remedy"
    runtime = composition.diagnostics()["runtime"]
    assert isinstance(runtime, dict)
    assert runtime["control_db"]["ping"] == "failed"
