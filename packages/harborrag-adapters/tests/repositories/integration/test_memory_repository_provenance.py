"""Integration coverage for memory user identity, provenance, and validity (0023).

Split from ``test_memory_repository.py`` for the file-length gate; the
``USER``-scope case here is security-relevant: it must key on ``user_id``,
not on the credential (``principal_id``) that wrote the memory.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.memory import SqlMemoryRepository
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryScope,
    MemoryType,
    new_memory_id,
)

pytestmark = pytest.mark.integration


def _memory(owner: MemoryOwner, scope: MemoryScope, content: str) -> Memory:
    now = datetime.now(UTC)
    return Memory(
        memory_id=new_memory_id(),
        scope=scope,
        memory_type=MemoryType.FACT,
        owner=owner,
        content=content,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_user_scope_isolates_by_user_id_across_principals(tmp_path: Path) -> None:
    """USER scope keys on user_id: the same human via another credential sees it,
    a different human via the same credential does not."""

    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        repo = SqlMemoryRepository(sessions)
        owner = MemoryOwner(tenant_id="ACME", user_id="alice", principal_id="api-key-1")
        memory = _memory(owner, MemoryScope.USER, "alice prefers French")
        await repo.save(memory)

        same_user_other_principal = MemoryOwner(
            tenant_id="ACME", user_id="alice", principal_id="api-key-2"
        )
        results = await repo.search(
            MemoryQuery(owner=same_user_other_principal, scopes=(MemoryScope.USER,))
        )
        assert [item.memory_id for item in results] == [memory.memory_id]
        loaded = await repo.get(same_user_other_principal, memory.memory_id)
        assert loaded is not None
        assert loaded.owner.user_id == "alice"
        assert loaded.owner.principal_id == "api-key-1"

        other_user_same_principal = MemoryOwner(
            tenant_id="ACME", user_id="bob", principal_id="api-key-1"
        )
        assert await repo.get(other_user_same_principal, memory.memory_id) is None
        results = await repo.search(
            MemoryQuery(owner=other_user_same_principal, scopes=(MemoryScope.USER,))
        )
        assert results == ()

        no_user = MemoryOwner(tenant_id="ACME", principal_id="api-key-1")
        assert await repo.search(MemoryQuery(owner=no_user, scopes=(MemoryScope.USER,))) == ()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_provenance_fields_round_trip(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        repo = SqlMemoryRepository(sessions)
        owner = MemoryOwner(tenant_id="ACME", user_id="alice")
        now = datetime.now(UTC)
        memory = replace(
            _memory(owner, MemoryScope.USER, "moved to Lyon"),
            valid_from=now - timedelta(days=1),
            invalid_at=now + timedelta(days=1),
            superseded_by="mem-next",
            source_session_id="session-1",
            source_message_ids=("msg-1", "msg-2"),
            entity_ids=("ent-lyon",),
            content_hash="sha256:abc",
        )
        await repo.save(memory)

        assert await repo.get(owner, memory.memory_id) == memory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_search_honours_validity_window_include_invalid_and_as_of(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        repo = SqlMemoryRepository(sessions)
        owner = MemoryOwner(tenant_id="ACME", user_id="alice")
        now = datetime.now(UTC)
        old = replace(
            _memory(owner, MemoryScope.USER, "lived in Paris"),
            valid_from=now - timedelta(days=30),
            invalid_at=now - timedelta(days=10),
            superseded_by="current",
        )
        current = replace(
            _memory(owner, MemoryScope.USER, "lives in Lyon"),
            memory_id="current",
            valid_from=now - timedelta(days=10),
        )
        future = replace(
            _memory(owner, MemoryScope.USER, "will live in Nice"),
            valid_from=now + timedelta(days=10),
        )
        timeless = _memory(owner, MemoryScope.USER, "likes cheese")
        for memory in (old, current, future, timeless):
            await repo.save(memory)

        def ids(results: tuple[Memory, ...]) -> set[str]:
            return {item.memory_id for item in results}

        query = MemoryQuery(owner=owner, scopes=(MemoryScope.USER,))
        assert ids(await repo.search(query)) == {current.memory_id, timeless.memory_id}
        assert ids(await repo.search(replace(query, include_invalid=True))) == {
            old.memory_id,
            current.memory_id,
            future.memory_id,
            timeless.memory_id,
        }
        assert ids(await repo.search(replace(query, as_of=now - timedelta(days=20)))) == {
            old.memory_id,
            timeless.memory_id,
        }
        assert ids(await repo.search(replace(query, as_of=now + timedelta(days=20)))) == {
            current.memory_id,
            future.memory_id,
            timeless.memory_id,
        }
        # get() by id is provenance access: superseded history stays readable.
        assert await repo.get(owner, old.memory_id) is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_search_order_is_total_and_deterministic(tmp_path: Path) -> None:
    """Importance, then recency, then memory_id: a limited page never depends
    on storage order."""

    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        repo = SqlMemoryRepository(sessions)
        owner = MemoryOwner(tenant_id="ACME", user_id="alice")
        now = datetime.now(UTC)
        tied = [
            replace(
                _memory(owner, MemoryScope.USER, f"tied {suffix}"),
                memory_id=f"mem-{suffix}",
                importance=0.5,
                created_at=now,
                updated_at=now,
            )
            for suffix in ("c", "a", "b")
        ]
        important = replace(
            _memory(owner, MemoryScope.USER, "top"),
            memory_id="mem-z",
            importance=0.9,
            created_at=now - timedelta(days=1),
            updated_at=now - timedelta(days=1),
        )
        for memory in (*tied, important):
            await repo.save(memory)

        query = MemoryQuery(owner=owner, scopes=(MemoryScope.USER,))
        results = await repo.search(query)
        assert [item.memory_id for item in results] == [
            "mem-z",
            "mem-a",
            "mem-b",
            "mem-c",
        ]
        page = await repo.search(replace(query, limit=2))
        assert [item.memory_id for item in page] == ["mem-z", "mem-a"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_content_hash_survives_search_and_is_indexed(tmp_path: Path) -> None:
    """Recall dedupes restatements by hash, so search must return it and the
    column must be indexed for lookups by it."""

    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        repo = SqlMemoryRepository(sessions)
        owner = MemoryOwner(tenant_id="ACME", user_id="alice")
        stored = replace(
            _memory(owner, MemoryScope.USER, "lives in Lyon"),
            content_hash="sha256:lyon",
        )
        await repo.save(stored)

        results = await repo.search(MemoryQuery(owner=owner, scopes=(MemoryScope.USER,)))
        assert [item.content_hash for item in results] == ["sha256:lyon"]

        async with sessions() as session:
            matched = await session.execute(
                sa.text("select memory_id from memories where content_hash = :hash"),
                {"hash": "sha256:lyon"},
            )
            assert [row[0] for row in matched] == [stored.memory_id]
            indexes = await session.execute(
                sa.text(
                    "select name from sqlite_master where type = 'index' and tbl_name = 'memories'"
                )
            )
            assert "ix_memories_content_hash" in {row[0] for row in indexes}
    finally:
        await engine.dispose()
