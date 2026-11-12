"""tenant_has_data looks for a project, a connection or a settings revision in one tenant."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from harborrag_adapters.repositories.database.control_plane.config_store import SqlSettingsStore
from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_core.config_store import SettingChange
from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.domain.project import Project
from harborrag_runtime.application.control_plane.connection_models import ConnectionKey
from harborrag_runtime.application.control_plane.tenants import tenant_has_data
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.testing.connection_fakes import (
    JIRA_WRITE,
    FakeSecrets,
    connection_world,
    fake_control_plane,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def store(tmp_path: Path) -> SqlSettingsStore:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'control.db'}"
    run_migrations(dsn)
    return SqlSettingsStore(
        async_sessionmaker(create_control_plane_engine(dsn), expire_on_commit=False)
    )


def _plane(store: SqlSettingsStore | None, *projects: Project) -> ControlPlaneRepositories:
    return replace(fake_control_plane(list(projects), FakeSecrets()), config_store=store)


async def test_a_tenant_with_a_project_has_data(store: SqlSettingsStore) -> None:
    plane = _plane(store, Project(id="a", name="A", collection="a", tenant_id="acme"))

    assert await tenant_has_data(plane, "acme") is True
    assert await tenant_has_data(plane, "other") is False


async def test_a_tenant_with_only_a_connection_has_data(store: SqlSettingsStore) -> None:
    world = connection_world([Project(id="eng", name="Eng", collection="e", tenant_id="acme")])
    await world.service.put(ConnectionKey("acme", "eng", "jira"), JIRA_WRITE, actor="u_demo")
    await world.control_plane.projects.delete("eng", tenant_ids=frozenset({"acme"}))
    plane = replace(world.control_plane, config_store=store)

    assert await tenant_has_data(plane, "acme") is True


async def test_a_tenant_with_only_a_settings_revision_has_data(store: SqlSettingsStore) -> None:
    change = SettingChange("tenant", "chunking.max_tokens", 1200, 512, "next_ingestion")
    await store.write("tuned", [change], expected_revision=0, source="seed", created_by="system")
    plane = _plane(store)

    assert await tenant_has_data(plane, "tuned") is True
    assert await tenant_has_data(plane, "new") is False


async def test_a_tenant_without_a_project_or_connection_needs_the_settings_store() -> None:
    with pytest.raises(HarborUnavailableError):
        await tenant_has_data(_plane(None), "new")
