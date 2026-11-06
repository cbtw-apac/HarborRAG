"""`harborrag config import|export` over a migrated SQLite control database."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from app_test_connections import FakeSecrets, fake_control_plane
from app_test_fixtures import MockAppService
from project_test_support import scaffold
from sqlalchemy.ext.asyncio import async_sessionmaker

from harborrag_adapters.repositories.database.control_plane.config_store import SqlSettingsStore
from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_app.workflow_control.control_plane.settings_import import SettingsImporter
from harborrag_core.config_store import SettingsLevel, SettingsRegistry
from harborrag_core.config_store.documents import plan_import
from harborrag_runtime.config.settings_document import load_settings_document
from harborrag_runtime.config_store import CachedConfigProvider

_TENANT = "DEFAULT"
_KEY = "chunking.max_tokens"
_STORED = f"{_KEY}: 384\n"


class _ConfigApp(MockAppService):
    def __init__(self, importer: SettingsImporter) -> None:
        super().__init__()
        self.settings_importer = importer


@dataclass
class _World:
    store: SqlSettingsStore
    directory: Path

    def file(self, text: str, name: str = "settings.yaml") -> Path:
        path = self.directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def seed(self, text: str) -> None:
        document = load_settings_document(self.file(text, "seed.yaml"))

        async def write() -> None:
            plan = plan_import(
                SettingsRegistry.default(), await self.store.load(_TENANT), document, prune=False
            )
            await self.store.write(
                _TENANT, plan.changes, expected_revision=0, source="seed", created_by="system"
            )

        asyncio.run(write())

    def revision(self) -> int:
        return asyncio.run(self.store.latest_revision(_TENANT))

    def overrides(self) -> dict[str, object]:
        return dict(asyncio.run(self.store.load(_TENANT)).overrides.get("tenant", {}))


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _World:
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
    importer = SettingsImporter(lambda: plane, registry)
    monkeypatch.setattr(cli_runner, "runtime_app_service", lambda: _ConfigApp(importer))
    monkeypatch.chdir(scaffold(tmp_path / "project"))
    return _World(store, tmp_path)


def _run(capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = cli.main(["config", *arguments])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _json(capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, dict[str, Any]]:
    code, out, _ = _run(capsys, *arguments, "--json")
    return code, json.loads(out)


def test_import_dry_run_prints_diff_and_changes_nothing(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.seed(f"{_KEY}: 512\n")
    path = world.file(_STORED)

    code, out, _ = _run(capsys, "import", str(path))

    assert code == 0
    for expected in (_KEY, "512", "384", "next_ingestion"):
        assert expected in out
    assert world.revision() == 1


def test_import_apply_writes_one_import_revision(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _json(capsys, "import", str(world.file(_STORED)), "--apply")

    revision = asyncio.run(world.store.revision(_TENANT, 1))
    assert code == 0
    assert world.revision() == 1
    assert revision is not None
    assert revision.source == "import"
    assert "@" in revision.created_by


def test_import_apply_again_prints_no_changes(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    path = world.file(_STORED)
    _run(capsys, "import", str(path), "--apply")

    code, out, _ = _run(capsys, "import", str(path), "--apply")

    assert code == 0
    assert "no changes" in out
    assert world.revision() == 1


def test_import_keeps_missing_key_unless_prune(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.seed(f"{_KEY}: 512\ningestion.concurrency: 4\n")
    path = world.file(_STORED)

    _run(capsys, "import", str(path), "--apply")
    kept = world.overrides()
    _run(capsys, "import", str(path), "--apply", "--prune")
    pruned = world.overrides()

    assert kept == {_KEY: 384, "ingestion.concurrency": 4}
    assert pruned == {_KEY: 384}


def test_import_bad_value_exits_non_zero(world: _World, capsys: pytest.CaptureFixture[str]) -> None:
    path = world.file(f'{_KEY}: "big"\n')

    code, out = _json(capsys, "import", str(path), "--apply")

    assert code != 0
    assert str(path) in out["error"]
    assert _KEY in out["error"]
    assert world.revision() == 0


def test_import_reports_and_skips_boot_only_key(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    path = world.file("database.url: postgresql://example/db\n")

    code, out, _ = _run(capsys, "import", str(path), "--apply")

    assert code == 0
    assert "skipped tenant database.url: boot-only" in out
    assert world.revision() == 0


def test_export_round_trip_reports_no_changes(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.seed(f"{_KEY}: 512\nretrieval.top_k: 7\n")
    exported = world.directory / "export.yaml"

    code, _, _ = _run(capsys, "export", "--output", str(exported))
    _, out, _ = _run(capsys, "import", str(exported))

    assert code == 0
    assert exported.read_text(encoding="utf-8") == f"{_KEY}: 512\nretrieval.top_k: 7\n"
    assert "no changes" in out


def test_export_prints_one_level_to_stdout(
    world: _World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.seed(f"{_KEY}: 512\n")

    code, out, _ = _run(capsys, "export", "--level", str(SettingsLevel("tenant")))

    assert code == 0
    assert out == f"{_KEY}: 512\n"
