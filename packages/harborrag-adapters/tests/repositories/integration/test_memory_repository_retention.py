"""Erasure reaches expired rows, a session filter narrows erasure, and retention lists by age."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harborrag_adapters.repositories.database.control_plane.memory import SqlMemoryRepository
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryScope,
    MemoryType,
)

pytestmark = [pytest.mark.integration, pytest.mark.whitebox]

_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _stored(  # noqa: PLR0913 - one argument per field a retention case pins
    memory_id: str,
    *,
    scope: MemoryScope = MemoryScope.USER,
    user_id: str = "user-1",
    updated_at: datetime = _NOW,
    expires_at: datetime | None = None,
    source_session_id: str | None = None,
) -> Memory:
    return Memory(
        memory_id=memory_id,
        scope=scope,
        memory_type=MemoryType.FACT,
        owner=MemoryOwner(
            tenant_id="ACME",
            user_id=user_id,
            session_id="session-1" if scope is MemoryScope.SESSION else None,
        ),
        content=f"synthetic fact {memory_id}",
        created_at=updated_at,
        updated_at=updated_at,
        expires_at=expires_at,
        source_session_id=source_session_id,
    )


@pytest.mark.asyncio
async def test_erasure_queries_reach_expired_rows_that_reads_never_see(
    sessions: SessionFactory,
) -> None:
    repo = SqlMemoryRepository(sessions)
    lapsed = _stored(
        "lapsed",
        updated_at=_NOW - timedelta(days=2),
        expires_at=_NOW - timedelta(days=1),
    )
    await repo.save(lapsed)
    owner = MemoryOwner(tenant_id="ACME", user_id="user-1")

    assert await repo.search(MemoryQuery(owner=owner, include_invalid=True)) == ()
    erasure = MemoryQuery(owner=owner, include_invalid=True, stored_by_owner=True)
    assert [row.memory_id for row in await repo.search(erasure)] == ["lapsed"]


@pytest.mark.asyncio
async def test_erasure_reaching_expired_rows_stays_inside_the_owners_tenant_and_user(
    sessions: SessionFactory,
) -> None:
    repo = SqlMemoryRepository(sessions)
    lapsed = {"updated_at": _NOW - timedelta(days=2), "expires_at": _NOW - timedelta(days=1)}
    await repo.save(_stored("mine", **lapsed))
    await repo.save(_stored("theirs", user_id="user-2", **lapsed))
    query = MemoryQuery(
        owner=MemoryOwner(tenant_id="OPS", user_id="user-1"),
        include_invalid=True,
        stored_by_owner=True,
    )

    assert await repo.search(query) == ()


@pytest.mark.asyncio
async def test_a_source_session_filter_narrows_an_erasure_query(sessions: SessionFactory) -> None:
    repo = SqlMemoryRepository(sessions)
    await repo.save(_stored("from-1", source_session_id="session-1"))
    await repo.save(_stored("from-2", source_session_id="session-2"))
    await repo.save(_stored("unsourced"))
    query = MemoryQuery(
        owner=MemoryOwner(tenant_id="ACME", user_id="user-1"),
        include_invalid=True,
        stored_by_owner=True,
        source_session_id="session-1",
    )

    assert [row.memory_id for row in await repo.search(query)] == ["from-1"]


@pytest.mark.asyncio
async def test_list_expired_returns_rows_past_their_scope_cutoff_or_expiry_oldest_first(
    sessions: SessionFactory,
) -> None:
    repo = SqlMemoryRepository(sessions)
    await repo.save(_stored("user-old", updated_at=_NOW - timedelta(days=400)))
    await repo.save(_stored("user-fresh", updated_at=_NOW - timedelta(days=10)))
    await repo.save(
        _stored("session-old", scope=MemoryScope.SESSION, updated_at=_NOW - timedelta(days=100))
    )
    await repo.save(
        _stored("tenant-old", scope=MemoryScope.TENANT, updated_at=_NOW - timedelta(days=900))
    )
    await repo.save(
        _stored(
            "explicitly-expired",
            scope=MemoryScope.TENANT,
            updated_at=_NOW - timedelta(days=3),
            expires_at=_NOW - timedelta(days=1),
        )
    )
    cutoffs = {
        MemoryScope.USER: _NOW - timedelta(days=365),
        MemoryScope.SESSION: _NOW - timedelta(days=90),
    }

    expired = await repo.list_expired(cutoffs=cutoffs, now=_NOW, limit=10)

    assert [row.memory_id for row in expired] == ["user-old", "session-old", "explicitly-expired"]
    assert [
        row.memory_id for row in await repo.list_expired(cutoffs=cutoffs, now=_NOW, limit=1)
    ] == ["user-old"]


@pytest.mark.asyncio
async def test_list_expired_returns_nothing_when_retention_is_not_enabled(
    sessions: SessionFactory,
) -> None:
    repo = SqlMemoryRepository(sessions)
    await repo.save(_stored("ancient", updated_at=_NOW - timedelta(days=5000)))
    await repo.save(
        _stored(
            "stale-expiry",
            updated_at=_NOW - timedelta(days=3),
            expires_at=_NOW - timedelta(days=1),
        )
    )

    assert await repo.list_expired(cutoffs={}, now=_NOW, limit=10) == ()
