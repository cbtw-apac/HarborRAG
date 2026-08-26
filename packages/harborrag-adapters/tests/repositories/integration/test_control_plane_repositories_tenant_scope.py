"""Cross-tenant isolation for the control-plane Sql* repositories.

Split out of test_control_plane_repositories.py to keep that file under the
repo's file-length gate; these tests are the SQL-level regression for the
critical control-plane tenant-scoping gap (list/get/delete previously had no
tenant_id filter at all).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.jobs import SqlJobRepository
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_adapters.repositories.database.control_plane.projects import (
    SqlProjectRepository,
    SqlSourceRepository,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.domain.job import Job
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
async def test_project_repository_enforces_tenant_scope(sessions: SessionFactory) -> None:
    """list/get/delete must not see or touch another tenant's project rows."""
    repo = SqlProjectRepository(sessions)
    await repo.create(Project(id="mine", tenant_id="tenant-a", name="Mine", collection="mine"))
    await repo.create(
        Project(id="theirs", tenant_id="tenant-b", name="Theirs", collection="theirs")
    )
    scope = frozenset({"tenant-a"})

    assert [p.id for p in await repo.list(tenant_ids=scope)] == ["mine"]
    assert await repo.get("theirs", tenant_ids=scope) is None
    assert (await repo.get("mine", tenant_ids=scope)) is not None

    await repo.delete("theirs", tenant_ids=scope)
    assert (await repo.get("theirs", tenant_ids=None)) is not None  # untouched

    await repo.delete("mine", tenant_ids=scope)
    assert await repo.get("mine", tenant_ids=None) is None  # in-scope delete works


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_source_repository_enforces_tenant_scope(sessions: SessionFactory) -> None:
    """list/get/delete must not see or touch another tenant's source rows."""
    projects = SqlProjectRepository(sessions)
    await projects.create(Project(id="p1", tenant_id="tenant-a", name="A", collection="a"))
    await projects.create(Project(id="p2", tenant_id="tenant-b", name="B", collection="b"))
    repo = SqlSourceRepository(sessions)
    await repo.create(
        SourceConfig(
            id="mine", tenant_id="tenant-a", project_id="p1", source_type="local_file", name="Mine"
        )
    )
    await repo.create(
        SourceConfig(
            id="theirs", tenant_id="tenant-b", project_id="p2", source_type="github", name="Theirs"
        )
    )
    scope = frozenset({"tenant-a"})

    assert [s.id for s in await repo.list(tenant_ids=scope)] == ["mine"]
    assert await repo.get("theirs", tenant_ids=scope) is None
    await repo.delete("theirs", tenant_ids=scope)
    assert (await repo.get("theirs", tenant_ids=None)) is not None  # untouched


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_job_repository_enforces_tenant_scope(sessions: SessionFactory) -> None:
    """list/get/count_by_status must not see another tenant's job rows."""
    repo = SqlJobRepository(sessions)
    await repo.save(
        Job(
            id="mine", tenant_id="tenant-a", source_id="s1", project_id="p1", job_type="bulk_ingest"
        )
    )
    await repo.save(
        Job(
            id="theirs",
            tenant_id="tenant-b",
            source_id="s1",
            project_id="p1",
            job_type="bulk_ingest",
        )
    )
    scope = frozenset({"tenant-a"})

    assert [j.id for j in await repo.list(tenant_ids=scope)] == ["mine"]
    assert await repo.get("theirs", tenant_ids=scope) is None
    assert await repo.count_by_status(tenant_ids=scope) == {"queued": 1}
