"""SqlGraphConflictRepository round-trips on SQLite.

Split out of test_control_plane_repository_crud.py to keep that file under
the repo's file-length gate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.graph_conflicts import (
    SqlGraphConflictRepository,
)
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.contracts.errors import HarborConflictError, HarborNotFoundError
from harborrag_core.domain.graph_conflict import GraphConflict

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[SessionFactory]:
    """Migrated SQLite-file DB and a session factory, torn down per test."""

    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    yield create_session_factory(engine)
    await engine.dispose()


def _conflict(
    conflict_id: str, *, detected_at: datetime, tenant_id: str = "tenant-a"
) -> GraphConflict:
    return GraphConflict(
        id=conflict_id,
        tenant_id=tenant_id,
        conflict_type="node_identity",
        subject_node_key="document:1",
        competing_node_key="document:2",
        description="Two sources describe the same entity differently",
        detected_at=detected_at,
    )


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_graph_conflict_repository_roundtrip_and_resolve(sessions: SessionFactory) -> None:
    """report/get/resolve against real SQL; resolving twice is a conflict, not silent."""

    repo = SqlGraphConflictRepository(sessions)
    conflict = _conflict("gc_1", detected_at=datetime(2026, 8, 12, 0, 0, 0, tzinfo=UTC))

    await repo.report(conflict)

    fetched = await repo.get("gc_1", tenant_ids=frozenset({"tenant-a"}))
    assert fetched is not None
    assert fetched.status == "open"
    assert fetched.action is None

    assert await repo.get("gc_1", tenant_ids=frozenset({"other-tenant"})) is None

    resolved = await repo.resolve(
        "gc_1", action="merge", resolved_by="alice", tenant_ids=frozenset({"tenant-a"})
    )
    assert resolved.status == "resolved"
    assert resolved.action == "merge"
    assert resolved.resolved_by == "alice"
    assert resolved.resolved_at is not None

    with pytest.raises(HarborConflictError):
        await repo.resolve(
            "gc_1", action="skip", resolved_by="bob", tenant_ids=frozenset({"tenant-a"})
        )

    with pytest.raises(HarborNotFoundError):
        await repo.resolve(
            "does-not-exist", action="skip", resolved_by="bob", tenant_ids=frozenset({"tenant-a"})
        )


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_graph_conflict_repository_list_pages_newest_first_via_cursor(
    sessions: SessionFactory,
) -> None:
    """list orders newest-detected first and its cursor walks the rest of the set."""

    repo = SqlGraphConflictRepository(sessions)
    conflicts = [
        _conflict(f"gc_{i}", detected_at=datetime(2026, 8, 12, 0, 0, i, tzinfo=UTC))
        for i in range(5)
    ]
    for conflict in conflicts:
        await repo.report(conflict)

    first_page, cursor = await repo.list(tenant_ids=None, cursor=None, limit=2)
    assert [c.id for c in first_page] == ["gc_4", "gc_3"]
    assert cursor is not None

    second_page, cursor = await repo.list(tenant_ids=None, cursor=cursor, limit=2)
    assert [c.id for c in second_page] == ["gc_2", "gc_1"]
    assert cursor is not None

    third_page, cursor = await repo.list(tenant_ids=None, cursor=cursor, limit=2)
    assert [c.id for c in third_page] == ["gc_0"]
    assert cursor is None


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_graph_conflict_repository_list_scopes_by_tenant(sessions: SessionFactory) -> None:
    """list only returns conflicts belonging to the caller's tenants."""

    repo = SqlGraphConflictRepository(sessions)
    await repo.report(
        _conflict("gc_a", detected_at=datetime(2026, 8, 12, 0, 0, 0, tzinfo=UTC), tenant_id="a")
    )
    await repo.report(
        _conflict("gc_b", detected_at=datetime(2026, 8, 12, 0, 0, 1, tzinfo=UTC), tenant_id="b")
    )

    scoped, _ = await repo.list(tenant_ids=frozenset({"a"}), cursor=None, limit=50)
    assert [c.id for c in scoped] == ["gc_a"]
