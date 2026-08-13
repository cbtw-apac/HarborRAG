"""Control-plane SQLAlchemy repository round-trips on SQLite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.jobs import SqlJobRepository
from harborrag_adapters.repositories.database.control_plane.leases import SqlLeaseRepository
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_adapters.repositories.database.control_plane.pending_effects import (
    SqlPendingEffectRepository,
)
from harborrag_adapters.repositories.database.control_plane.projects import (
    SqlProjectRepository,
    SqlSourceRepository,
)
from harborrag_adapters.repositories.database.control_plane.secrets import SqlSecretsRepository
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.contracts.errors import (
    HarborConflictError,
    HarborNotFoundError,
    HarborSecretDecryptionError,
)
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.job import Job
from harborrag_core.domain.pending_effect import PendingControlPlaneEffect
from harborrag_core.domain.project import Project
from harborrag_core.domain.source_config import SourceConfig

pytestmark = pytest.mark.integration


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
async def test_project_repository_roundtrip(sessions: SessionFactory) -> None:
    """Project create/get/list/update/delete against real SQL."""

    repo = SqlProjectRepository(sessions)
    project = Project(id="p1", tenant_id="tenant-a", name="Docs", collection="docs_main")
    await repo.create(project)
    fetched = await repo.get("p1", tenant_ids=None)
    assert fetched == project
    project.name = "Docs v2"
    await repo.update(project)
    updated = await repo.get("p1", tenant_ids=None)
    assert updated is not None and updated.name == "Docs v2"
    assert updated.updated_at >= project.created_at
    assert [p.id for p in await repo.list(tenant_ids=None)] == ["p1"]
    project.tenant_id = "tenant-b"
    with pytest.raises(HarborConflictError, match="tenant identity is immutable"):
        await repo.update(project)
    await repo.delete("p1", tenant_ids=None)
    assert await repo.get("p1", tenant_ids=None) is None


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
    assert await repo.get("s1", tenant_ids=None) == source
    assert [s.id for s in await repo.list(project_id="p1", tenant_ids=None)] == ["s1"]
    assert len(await repo.list(tenant_ids=None)) == 2
    source.status = "paused"
    await repo.update(source)
    paused = await repo.get("s1", tenant_ids=None)
    assert paused is not None and paused.status == "paused"
    source.tenant_id = "tenant-b"
    with pytest.raises(HarborConflictError, match="tenant identity is immutable"):
        await repo.update(source)
    await repo.delete("s2", tenant_ids=None)
    assert await repo.get("s2", tenant_ids=None) is None


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_secrets_repository_flags_decryption_failure_as_distinct_from_not_found(
    sessions: SessionFactory,
) -> None:
    """A ref encrypted under a since-rotated key must not be conflated with a missing ref."""

    repo = SqlSecretsRepository(sessions, encryption_key="original-key")
    ref = await repo.put("hunter2")
    assert await repo.resolve(ref) == "hunter2"

    rotated = SqlSecretsRepository(sessions, encryption_key="rotated-key")
    with pytest.raises(HarborSecretDecryptionError, match="cannot be decrypted"):
        await rotated.resolve(ref)

    # A genuinely missing ref still 404s rather than raising the decryption error.
    with pytest.raises(HarborNotFoundError):
        await rotated.resolve("secret://db/does-not-exist")


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_job_repository_roundtrip_and_event_log(sessions: SessionFactory) -> None:
    """Job save/get/list filters + ordered per-job event sequence numbers."""

    repo = SqlJobRepository(sessions)
    job = Job(
        id="j1",
        tenant_id="tenant-a",
        source_id="s1",
        project_id="p1",
        job_type="bulk_ingest",
    )
    await repo.save(job)
    assert await repo.get("j1", tenant_ids=None) == job
    job.status = "running"
    job.attempts = 1
    await repo.save(job)
    assert [j.id for j in await repo.list(status="running", tenant_ids=None)] == ["j1"]
    assert await repo.list(status="failed", tenant_ids=None) == []
    assert [j.id for j in await repo.list(source_id="s1", tenant_ids=None)] == ["j1"]

    await repo.save(
        Job(
            id="j2",
            tenant_id="tenant-a",
            source_id="s1",
            project_id="p1",
            job_type="bulk_ingest",
        )
    )
    assert await repo.count_by_status(tenant_ids=None) == {"running": 1, "queued": 1}

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
async def test_pending_effect_repository_roundtrip_is_oldest_first_and_completion_removes_it(
    sessions: SessionFactory,
) -> None:
    """enqueue/list_pending/complete against real SQL; completion is a hard delete."""

    repo = SqlPendingEffectRepository(sessions)
    # Explicit, distinct timestamps make the ordering assertion independent
    # of enqueue order or the local clock's resolution.
    first = PendingControlPlaneEffect(
        id="eff_1",
        kind="retire_secret",
        payload={"ref": "secret://db/1"},
        created_at=datetime(2026, 8, 12, 0, 0, 1, tzinfo=UTC),
    )
    second = PendingControlPlaneEffect(
        id="eff_2",
        kind="log_activity",
        payload={"id": "act_1", "summary": "Created source 'Docs'"},
        created_at=datetime(2026, 8, 12, 0, 0, 0, tzinfo=UTC),
    )
    await repo.enqueue(first)
    await repo.enqueue(second)

    pending = await repo.list_pending()

    assert [effect.id for effect in pending] == ["eff_2", "eff_1"]  # second is older
    assert pending[0].payload == second.payload
    assert pending[0].created_at <= pending[1].created_at

    await repo.complete("eff_2")

    remaining = await repo.list_pending()
    assert [effect.id for effect in remaining] == ["eff_1"]

    await repo.complete("eff_2")  # already gone: a no-op, not an error
    assert [effect.id for effect in await repo.list_pending()] == ["eff_1"]


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_lease_repository_grants_exactly_one_live_holder_at_a_time(
    sessions: SessionFactory,
) -> None:
    """A rival holder is refused while the lease is live, but wins it once it lapses."""

    repo = SqlLeaseRepository(sessions)
    name = "ingestion_progress_bridge"  # seeded (already-expired) by migration 0017

    assert await repo.try_acquire(name, "holder-a", ttl_seconds=1000) is True
    assert await repo.try_acquire(name, "holder-b", ttl_seconds=1000) is False  # still held by a
    assert await repo.try_acquire(name, "holder-a", ttl_seconds=1000) is True  # a renews

    assert await repo.try_acquire(name, "holder-a", ttl_seconds=-1) is True  # a's lease lapses
    assert await repo.try_acquire(name, "holder-b", ttl_seconds=1000) is True  # b takes over
    assert await repo.try_acquire(name, "holder-a", ttl_seconds=1000) is False  # a is now the rival

    assert await repo.try_acquire("no_such_lease", "holder-a", ttl_seconds=1000) is False
