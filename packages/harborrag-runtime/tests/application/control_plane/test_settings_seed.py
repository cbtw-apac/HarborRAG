"""First-boot seed of ``config/settings.yaml`` and drift reporting over a migrated SQLite store."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from harborrag_adapters.repositories.database.control_plane.config_store import SqlSettingsStore
from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_core.config_store import SettingsLevel, SettingsRegistry, SettingsSnapshot
from harborrag_runtime.application.control_plane.settings_import import (
    SettingsImporter,
    seed_settings_once,
)
from harborrag_runtime.config_store import CachedConfigProvider
from harborrag_runtime.testing.connection_fakes import FakeSecrets, fake_control_plane

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_TENANT = "acme"
_KEY = "chunking.max_tokens"
_LEVEL = SettingsLevel("tenant")


@dataclass
class _World:
    importer: SettingsImporter
    store: SqlSettingsStore
    registry: SettingsRegistry
    path: Path

    async def seed(self) -> str:
        return await seed_settings_once(self.importer, self.path, tenant_id=_TENANT)

    def write_file(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    async def effective(self) -> tuple[object, str]:
        provider = CachedConfigProvider(self.store, self.registry)
        item = (await provider.effective(_TENANT, _LEVEL)).values[_KEY]
        return item.value, item.origin


@pytest.fixture
def world(tmp_path: Path) -> _World:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'control.db'}"
    run_migrations(dsn)
    store = SqlSettingsStore(
        async_sessionmaker(create_control_plane_engine(dsn), expire_on_commit=False)
    )
    registry = SettingsRegistry.default()
    plane = replace(
        fake_control_plane([], FakeSecrets()),
        config_store=store,
        config=CachedConfigProvider(store, registry),
    )
    return _World(
        SettingsImporter(lambda: plane, registry), store, registry, tmp_path / "settings.yaml"
    )


async def test_first_boot_seeds_once(world: _World) -> None:
    world.write_file(f"{_KEY}: 512\n")

    assert await world.seed() == "seeded"

    assert await world.effective() == (512, "tenant")
    revision = await world.store.revision(_TENANT, 1)
    assert await world.store.latest_revision(_TENANT) == 1
    assert revision is not None
    assert (revision.source, revision.created_by) == ("seed", "system")


async def test_later_boot_reports_drift_and_keeps_value(
    world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    world.write_file(f"{_KEY}: 512\n")
    await world.seed()
    world.write_file(f"{_KEY}: 1024\n")

    with caplog.at_level(logging.INFO):
        assert await world.seed() == "drift"

    assert await world.effective() == (512, "tenant")
    assert await world.store.latest_revision(_TENANT) == 1
    (warning,) = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert warning.__dict__["event"] == "control_plane.settings_file_drift"
    assert warning.__dict__["config_path"] == str(world.path)
    assert warning.__dict__["keys_count"] == 1
    assert _KEY not in warning.getMessage()


async def test_unchanged_file_after_seed_is_in_sync(world: _World) -> None:
    world.write_file(f"{_KEY}: 512\n")
    await world.seed()

    assert await world.seed() == "in_sync"


async def test_missing_file_seeds_nothing(world: _World) -> None:
    assert await world.seed() == "no_file"
    assert await world.store.latest_revision(_TENANT) == 0


async def test_invalid_seed_file_writes_nothing(
    world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    world.write_file(f"{_KEY}: big\nretrieval.top_k: 7\n")

    assert await world.seed() == "invalid"

    assert await world.store.latest_revision(_TENANT) == 0
    (error,) = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert error.__dict__["event"] == "control_plane.settings_seed_invalid"
    assert error.__dict__["config_path"] == str(world.path)
    assert "big" not in error.getMessage()


async def test_seed_race_writes_one_revision(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both seeds read revision 0 before either writes, so one of them must lose the race.

    Without the barrier the event loop may finish one seed before the other reads its
    snapshot; that seed then reports ``in_sync``, which the sequential test covers.
    """
    world.write_file(f"{_KEY}: 512\n")
    both_read = asyncio.Barrier(2)
    read_snapshot = world.importer.snapshot

    async def snapshot_then_wait(*, tenant_id: str) -> SettingsSnapshot:
        snapshot = await read_snapshot(tenant_id=tenant_id)
        await both_read.wait()
        return snapshot

    monkeypatch.setattr(world.importer, "snapshot", snapshot_then_wait)

    outcomes = await asyncio.gather(world.seed(), world.seed())

    assert set(outcomes) == {"seeded", "raced"}
    assert await world.store.latest_revision(_TENANT) == 1


async def test_unknown_project_in_seed_file_names_level(
    world: _World, caplog: pytest.LogCaptureFixture
) -> None:
    world.write_file("projects:\n  ghost:\n    retrieval.top_k: 7\n")

    assert await world.seed() == "invalid"

    (error,) = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert error.__dict__["event"] == "control_plane.settings_seed_invalid"
    assert error.__dict__["config_path"] == str(world.path)
    assert error.__dict__["settings_level_kind"] == "project"
    assert "ghost" not in error.getMessage()
    assert await world.store.latest_revision(_TENANT) == 0
