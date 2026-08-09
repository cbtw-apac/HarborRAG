"""Control-plane DB: migrations + SQLAlchemy repos round-trip on SQLite (ST5)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.jobs import (
    SqlActivityRepository,
)
from harborrag_adapters.repositories.database.control_plane.migrations import (
    _build_config,
    run_migrations,
)
from harborrag_adapters.repositories.database.control_plane.projects import (
    SqlProjectRepository,
    SqlSourceRepository,
)
from harborrag_adapters.repositories.database.control_plane.schemas import Base
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_adapters.repositories.database.control_plane.workspace import (
    SqlMemberRepository,
    SqlProviderRepository,
    SqlSettingsRepository,
)
from harborrag_adapters.repositories.database.ingestion_control.schema import (
    METADATA as INGESTION_METADATA,
)
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.member import Member
from harborrag_core.domain.project import Project
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.domain.source_config import SourceConfig

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "projects",
    "sources",
    "secrets",
    "jobs",
    "job_events",
    "ingestion_failures",
    "activity",
    "providers",
    "routing_rules",
    "workspace_settings",
    "members",
    "mcp_query_log",
    "conversation_memory",
    "conversation_sessions",
    "source_scopes",
    "source_scans",
    "source_items",
    "documents",
    "document_versions",
    "ingestion_tasks",
    "task_document_results",
    "document_failures",
    "projection_manifests",
    "projection_cleanup_jobs",
    "reindex_jobs",
}


@pytest.mark.whitebox
def test_migration_config_preserves_percent_encoded_credentials() -> None:
    dsn = "postgresql+asyncpg://harbor:p%40ss%25word@db:5432/harbor"

    config = _build_config(dsn)

    assert config.get_main_option("sqlalchemy.url") == dsn


@pytest_asyncio.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[SessionFactory]:
    """Migrated SQLite-file DB and a session factory, torn down per test."""
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_in_memory_engine_keeps_data_alive_across_connections() -> None:
    """In-memory SQLite must use a shared pool: each new connection off a
    NullPool engine opens its own private, empty `:memory:` database, so a
    session after the first would silently see no data at all."""
    engine = create_control_plane_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text("CREATE TABLE t (id INTEGER)"))
            await connection.execute(sa.text("INSERT INTO t VALUES (1)"))

        async with engine.connect() as connection:
            result = await connection.execute(sa.text("SELECT COUNT(*) FROM t"))
            assert result.scalar() == 1
    finally:
        await engine.dispose()


@pytest.mark.whitebox
def test_migrations_create_all_tables_and_are_idempotent(tmp_path: Path) -> None:
    """Migrations create both control planes; a second run is a no-op."""
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    run_migrations(dsn)  # idempotent: upgrade head twice must not fail
    sync_engine = sa.create_engine(f"sqlite:///{tmp_path}/control.db")
    tables = set(sa.inspect(sync_engine).get_table_names())
    sync_engine.dispose()
    assert EXPECTED_TABLES <= tables
    assert "alembic_version" in tables


@pytest.mark.whitebox
def test_migration_baseline_matches_current_metadata(tmp_path: Path) -> None:
    """The squashed baseline must not drift from either database metadata tree."""

    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    sync_engine = sa.create_engine(f"sqlite:///{tmp_path}/control.db")
    try:
        with sync_engine.connect() as connection:
            context = MigrationContext.configure(connection)
            differences = compare_metadata(
                context,
                [Base.metadata, INGESTION_METADATA],
            )
    finally:
        sync_engine.dispose()
    assert differences == []


@pytest.mark.whitebox
def test_legacy_0007_database_upgrades_to_authoritative_tenancy(
    tmp_path: Path,
) -> None:
    """The squashed baseline must retain a working upgrade path from 0007.

    A database created from the consolidated baseline (0001) already has
    ``tenant_id`` before 0008 ever runs, so downgrading to 0007 must not drop
    it -- there is no schema-only signal to tell "the baseline always had
    this column" apart from "0008's upgrade() just added it", and dropping
    unconditionally would strip tenant scoping from a freshly bootstrapped
    database (see 0008's downgrade() docstring). The column must survive a
    downgrade-then-upgrade round trip intact.
    """

    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    config = _build_config(dsn)
    command.downgrade(config, "0007")

    sync_engine = sa.create_engine(f"sqlite:///{tmp_path}/control.db")
    try:
        inspector = sa.inspect(sync_engine)
        for table_name in ("source_scopes", "documents", "ingestion_tasks"):
            assert "tenant_id" in {column["name"] for column in inspector.get_columns(table_name)}
    finally:
        sync_engine.dispose()

    command.upgrade(config, "head")

    sync_engine = sa.create_engine(f"sqlite:///{tmp_path}/control.db")
    try:
        inspector = sa.inspect(sync_engine)
        for table_name in ("source_scopes", "documents", "ingestion_tasks"):
            tenant = next(
                column
                for column in inspector.get_columns(table_name)
                if column["name"] == "tenant_id"
            )
            assert tenant["nullable"] is False
    finally:
        sync_engine.dispose()


@pytest.mark.whitebox
def test_consolidated_baseline_downgrades_cleanly(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)

    command.downgrade(_build_config(dsn), "base")

    sync_engine = sa.create_engine(f"sqlite:///{tmp_path}/control.db")
    try:
        assert set(sa.inspect(sync_engine).get_table_names()) <= {"alembic_version"}
    finally:
        sync_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_project_repository_roundtrip(sessions: SessionFactory) -> None:
    """Project create/get/list/update/delete against real SQL."""
    repo = SqlProjectRepository(sessions)
    project = Project(id="p1", tenant_id="tenant-a", name="Docs", collection="docs_main")
    await repo.create(project)
    fetched = await repo.get("p1")
    assert fetched == project
    project.name = "Docs v2"
    await repo.update(project)
    updated = await repo.get("p1")
    assert updated is not None and updated.name == "Docs v2"
    assert updated.updated_at >= project.created_at
    assert [p.id for p in await repo.list()] == ["p1"]
    project.tenant_id = "tenant-b"
    with pytest.raises(HarborConflictError, match="tenant identity is immutable"):
        await repo.update(project)
    await repo.delete("p1")
    assert await repo.get("p1") is None


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_source_repository_roundtrip_and_project_filter(
    sessions: SessionFactory,
) -> None:
    """Source CRUD + list(project_id=...) scoping; config keeps secret_refs only."""
    projects = SqlProjectRepository(sessions)
    await projects.create(Project(id="p1", tenant_id="tenant-a", name="A", collection="a"))
    await projects.create(Project(id="p2", tenant_id="tenant-b", name="B", collection="b"))
    repo = SqlSourceRepository(sessions)
    source = SourceConfig(
        id="s1",
        tenant_id="tenant-a",
        project_id="p1",
        source_type="local_file",
        name="docs",
        config={"path": "/data", "token": {"secret_ref": "secret://x"}},
        secret_refs=["secret://x"],
    )
    await repo.create(source)
    await repo.create(
        SourceConfig(
            id="s2",
            tenant_id="tenant-b",
            project_id="p2",
            source_type="github",
            name="repo",
        )
    )
    assert await repo.get("s1") == source
    assert [s.id for s in await repo.list(project_id="p1")] == ["s1"]
    assert len(await repo.list()) == 2
    source.status = "paused"
    await repo.update(source)
    paused = await repo.get("s1")
    assert paused is not None and paused.status == "paused"
    source.tenant_id = "tenant-b"
    with pytest.raises(HarborConflictError, match="tenant identity is immutable"):
        await repo.update(source)
    await repo.delete("s2")
    assert await repo.get("s2") is None


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_activity_settings_provider_member_roundtrips(
    sessions: SessionFactory,
) -> None:
    """Remaining repos: activity append/list, settings upsert, provider and
    member upsert/lookup/delete."""
    activity = SqlActivityRepository(sessions)
    await activity.append(
        ActivityEntry(
            id="a1",
            tenant_id="tenant-a",
            actor="nguyen.vu@cbtw.tech",
            verb="created",
            entity_type="project",
            entity_id="p1",
            summary="Created project Docs",
        )
    )
    entries = await activity.list()
    assert [e.id for e in entries] == ["a1"]
    assert entries[0].tenant_id == "tenant-a"

    settings = SqlSettingsRepository(sessions)
    assert (await settings.get()).data == {}
    await settings.put(WorkspaceSettings(tenant_id="tenant-a", data={"theme": "dark"}))
    stored_settings = await settings.get()
    assert stored_settings.tenant_id == "tenant-a"
    assert stored_settings.data == {"theme": "dark"}
    await settings.put(WorkspaceSettings(tenant_id="tenant-a", data={"theme": "light"}))
    assert (await settings.get()).data == {"theme": "light"}

    providers = SqlProviderRepository(sessions)
    provider = Provider(
        id="pr1",
        tenant_id="tenant-a",
        name="OpenAI",
        family="chat",
        secret_ref="secret://key",
    )
    await providers.save(provider)
    assert await providers.get("pr1") == provider
    provider.name = "OpenAI EU"
    await providers.save(provider)
    listed = await providers.list()
    assert listed[0].name == "OpenAI EU"
    await providers.delete("pr1")
    assert await providers.get("pr1") is None

    members = SqlMemberRepository(sessions)
    member = Member(id="m1", tenant_id="tenant-a", subject="user@cbtw.tech", role="editor")
    await members.save(member)
    assert await members.get_by_subject("user@cbtw.tech") == member
    assert await members.get_by_subject("ghost@cbtw.tech") is None
    assert [m.id for m in await members.list()] == ["m1"]
    await members.delete("m1")
    assert await members.list() == []
