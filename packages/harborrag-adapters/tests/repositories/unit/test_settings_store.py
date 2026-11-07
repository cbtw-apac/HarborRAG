"""SqlSettingsStore over a migrated SQLite control-plane database."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from harborrag_adapters.repositories.database.control_plane.config_store import (
    SqlSettingsStore,
    notify_statement,
)
from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
)
from harborrag_adapters.repositories.database.control_plane.migrations import (
    _build_config,
    run_migrations,
)
from harborrag_core.config_store import SettingChange
from harborrag_core.contracts.errors import HarborPreconditionFailedError

_TABLES = {"setting_overrides", "setting_revisions"}


def _changes() -> list[SettingChange]:
    return [
        SettingChange("tenant", "retrieval.top_k", 5, 10, "live"),
        SettingChange("project:cpm", "retrieval.top_k", 10, 20, "live"),
        SettingChange("connection:cpm/wiki", "ingestion.concurrency", 8, 4, "next_ingestion"),
    ]


async def _write(store: SqlSettingsStore, changes: list[SettingChange], expected: int) -> int:
    return await store.write(
        "acme", changes, expected_revision=expected, source="seed", created_by="system"
    )


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'control.db'}"
    run_migrations(dsn)
    control_engine = create_control_plane_engine(dsn)
    yield control_engine
    await control_engine.dispose()


@pytest.fixture
def store(engine: AsyncEngine) -> SqlSettingsStore:
    return SqlSettingsStore(async_sessionmaker(engine, expire_on_commit=False))


def test_settings_migration_round_trip(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'control.db'}"
    run_migrations(dsn)
    sync_url = f"sqlite:///{tmp_path / 'control.db'}"

    def tables() -> set[str]:
        engine = sa.create_engine(sync_url)
        try:
            return set(sa.inspect(engine).get_table_names())
        finally:
            engine.dispose()

    assert _TABLES <= tables()
    command.downgrade(_build_config(dsn), "0045")
    assert not _TABLES & tables()
    run_migrations(dsn)
    assert _TABLES <= tables()


@pytest.mark.asyncio
async def test_empty_tenant_is_revision_zero(store: SqlSettingsStore) -> None:
    snapshot = await store.load("acme")

    assert snapshot.revision == 0
    assert not snapshot.overrides
    assert await store.latest_revision("acme") == 0


@pytest.mark.asyncio
async def test_write_appends_revision_and_overrides(store: SqlSettingsStore) -> None:
    assert await _write(store, _changes(), 0) == 1

    snapshot = await store.load("acme")
    assert snapshot.revision == 1
    assert set(snapshot.overrides) == {"tenant", "project:cpm", "connection:cpm/wiki"}
    assert snapshot.overrides["project:cpm"] == {"retrieval.top_k": 20}
    logged = await store.revision("acme", 1)
    assert logged is not None
    assert logged.source == "seed"
    assert logged.created_by == "system"
    assert logged.changes == tuple(_changes())
    assert await store.revision("acme", 2) is None


@pytest.mark.asyncio
async def test_null_removes_override(store: SqlSettingsStore) -> None:
    await _write(store, _changes(), 0)

    revision = await _write(
        store, [SettingChange("project:cpm", "retrieval.top_k", 20, None, "live")], 1
    )

    assert revision == 2
    snapshot = await store.load("acme")
    assert "project:cpm" not in snapshot.overrides
    assert "tenant" in snapshot.overrides


@pytest.mark.asyncio
async def test_overwrite_updates_value(store: SqlSettingsStore) -> None:
    await _write(store, _changes(), 0)

    await _write(store, [SettingChange("tenant", "retrieval.top_k", 10, 50, "live")], 1)

    assert (await store.load("acme")).overrides["tenant"] == {"retrieval.top_k": 50}


@pytest.mark.asyncio
async def test_stale_expected_revision_fails(store: SqlSettingsStore) -> None:
    await _write(store, _changes(), 0)
    before = await store.load("acme")

    with pytest.raises(HarborPreconditionFailedError):
        await _write(store, [SettingChange("tenant", "retrieval.top_k", 10, 7, "live")], 0)

    assert await store.latest_revision("acme") == 1
    assert await store.load("acme") == before


@pytest.mark.asyncio
async def test_empty_changes_are_rejected(store: SqlSettingsStore) -> None:
    with pytest.raises(ValueError, match="changes"):
        await _write(store, [], 0)


@pytest.mark.asyncio
async def test_duplicate_revision_maps_to_precondition_failed(
    store: SqlSettingsStore, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _write(store, _changes(), 0)

    async def stale(*_args: object, **_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(SqlSettingsStore, "_current_revision", stale)
    with pytest.raises(HarborPreconditionFailedError):
        await _write(store, [SettingChange("tenant", "retrieval.top_k", 10, 7, "live")], 0)

    async with engine.connect() as connection:
        count = (
            await connection.execute(
                sa.text("SELECT count(*) FROM setting_revisions WHERE tenant_id = 'acme'")
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_tenants_are_isolated(store: SqlSettingsStore) -> None:
    await _write(store, _changes(), 0)

    snapshot = await store.load("globex")

    assert snapshot.revision == 0
    assert not snapshot.overrides
    assert await store.latest_revision("globex") == 0


@pytest.mark.asyncio
async def test_notify_only_on_postgres(store: SqlSettingsStore, engine: AsyncEngine) -> None:
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def spy(_conn, _cursor, statement, *_rest) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    await _write(store, _changes(), 0)

    assert statements
    assert not any("pg_notify" in statement for statement in statements)
    compiled = str(notify_statement("acme").compile(dialect=postgresql.dialect()))
    assert "pg_notify" in compiled


@pytest.mark.asyncio
async def test_ping_succeeds_on_reachable_store(store: SqlSettingsStore) -> None:
    await store.ping()


class _RecordingSession:
    """Async session double that records, in order, the isolation request and each read."""

    def __init__(self, dialect: str) -> None:
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        self.calls: list[str] = []

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def connection(self, *, execution_options: dict[str, object]) -> None:
        self.calls.append(f"isolation={execution_options.get('isolation_level')}")

    async def scalar(self, _statement: object) -> int:
        self.calls.append("revision")
        return 3

    async def scalars(self, _statement: object) -> SimpleNamespace:
        self.calls.append("overrides")
        return SimpleNamespace(all=list)


@pytest.mark.asyncio
async def test_load_reads_revision_and_overrides_in_one_snapshot_on_postgres() -> None:
    session = _RecordingSession("postgresql")
    store = SqlSettingsStore(cast(Any, lambda: session))

    snapshot = await store.load("acme")

    assert snapshot.revision == 3
    assert session.calls == ["isolation=REPEATABLE READ", "revision", "overrides"]


@pytest.mark.asyncio
async def test_load_leaves_sqlite_isolation_alone() -> None:
    session = _RecordingSession("sqlite")
    store = SqlSettingsStore(cast(Any, lambda: session))

    await store.load("acme")

    assert session.calls == ["revision", "overrides"]
