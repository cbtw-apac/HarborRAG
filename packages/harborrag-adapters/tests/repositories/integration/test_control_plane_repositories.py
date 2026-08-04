"""Control-plane DB: migrations + SQLAlchemy repos round-trip on SQLite (ST5)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.jobs import (
    SqlActivityRepository,
    SqlJobRepository,
)
from harborrag_adapters.repositories.database.control_plane.migrations import (
    run_migrations,
)
from harborrag_adapters.repositories.database.control_plane.projects import (
    SqlProjectRepository,
    SqlSourceRepository,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_adapters.repositories.database.control_plane.workspace import (
    SqlMemberRepository,
    SqlProviderRepository,
    SqlSettingsRepository,
)
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.job import Job
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
}


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
    """0001 creates the 12 plan §6 tables; a second run is a no-op."""
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    run_migrations(dsn)  # idempotent: upgrade head twice must not fail
    sync_engine = sa.create_engine(f"sqlite:///{tmp_path}/control.db")
    tables = set(sa.inspect(sync_engine).get_table_names())
    sync_engine.dispose()
    assert EXPECTED_TABLES <= tables
    assert "alembic_version" in tables


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_project_repository_roundtrip(sessions: SessionFactory) -> None:
    """Project create/get/list/update/delete against real SQL."""
    repo = SqlProjectRepository(sessions)
    project = Project(id="p1", name="Docs", collection="docs_main")
    await repo.create(project)
    fetched = await repo.get("p1")
    assert fetched == project
    project.name = "Docs v2"
    await repo.update(project)
    updated = await repo.get("p1")
    assert updated is not None and updated.name == "Docs v2"
    assert updated.updated_at >= project.created_at
    assert [p.id for p in await repo.list()] == ["p1"]
    await repo.delete("p1")
    assert await repo.get("p1") is None


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_source_repository_roundtrip_and_project_filter(
    sessions: SessionFactory,
) -> None:
    """Source CRUD + list(project_id=...) scoping; config keeps secret_refs only."""
    projects = SqlProjectRepository(sessions)
    await projects.create(Project(id="p1", name="A", collection="a"))
    await projects.create(Project(id="p2", name="B", collection="b"))
    repo = SqlSourceRepository(sessions)
    source = SourceConfig(
        id="s1",
        project_id="p1",
        source_type="local_file",
        name="docs",
        config={"path": "/data", "token": {"secret_ref": "secret://x"}},
        secret_refs=["secret://x"],
    )
    await repo.create(source)
    await repo.create(SourceConfig(id="s2", project_id="p2", source_type="github", name="repo"))
    assert await repo.get("s1") == source
    assert [s.id for s in await repo.list(project_id="p1")] == ["s1"]
    assert len(await repo.list()) == 2
    source.status = "paused"
    await repo.update(source)
    paused = await repo.get("s1")
    assert paused is not None and paused.status == "paused"
    await repo.delete("s2")
    assert await repo.get("s2") is None


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_job_repository_roundtrip_and_event_log(
    sessions: SessionFactory,
) -> None:
    """Job save/get/list filters + ordered per-job event sequence numbers."""
    repo = SqlJobRepository(sessions)
    job = Job(id="j1", source_id="s1", project_id="p1", job_type="bulk_ingest")
    await repo.save(job)
    assert await repo.get("j1") == job
    job.status = "running"
    job.attempts = 1
    await repo.save(job)
    assert [j.id for j in await repo.list(status="running")] == ["j1"]
    assert await repo.list(status="failed") == []
    assert [j.id for j in await repo.list(source_id="s1")] == ["j1"]

    await repo.save(Job(id="j2", source_id="s1", project_id="p1", job_type="bulk_ingest"))
    assert await repo.count_by_status() == {"running": 1, "queued": 1}

    await repo.append_event(
        "j1", HarborEvent(name="job_status", trace_id="t1", payload={"s": "running"})
    )
    await repo.append_event("j1", HarborEvent(name="job_status", trace_id="t2"))
    async with sessions() as session:
        seqs = list(
            await session.scalars(
                sa.text("SELECT seq FROM job_events WHERE job_id='j1' ORDER BY seq")
            )
        )
    assert seqs == [1, 2]


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
            actor="nguyen.vu@cbtw.tech",
            verb="created",
            entity_type="project",
            entity_id="p1",
            summary="Created project Docs",
        )
    )
    entries = await activity.list()
    assert [e.id for e in entries] == ["a1"]

    settings = SqlSettingsRepository(sessions)
    assert (await settings.get()).data == {}
    await settings.put(WorkspaceSettings(data={"theme": "dark"}))
    assert (await settings.get()).data == {"theme": "dark"}
    await settings.put(WorkspaceSettings(data={"theme": "light"}))
    assert (await settings.get()).data == {"theme": "light"}

    providers = SqlProviderRepository(sessions)
    provider = Provider(id="pr1", name="OpenAI", family="chat", secret_ref="secret://key")
    await providers.save(provider)
    assert await providers.get("pr1") == provider
    provider.name = "OpenAI EU"
    await providers.save(provider)
    listed = await providers.list()
    assert listed[0].name == "OpenAI EU"
    await providers.delete("pr1")
    assert await providers.get("pr1") is None

    members = SqlMemberRepository(sessions)
    member = Member(id="m1", subject="user@cbtw.tech", role="editor")
    await members.save(member)
    assert await members.get_by_subject("user@cbtw.tech") == member
    assert await members.get_by_subject("ghost@cbtw.tech") is None
    assert [m.id for m in await members.list()] == ["m1"]
    await members.delete("m1")
    assert await members.list() == []
