"""Integration coverage for the SQL canonical long-term memory store.

The scope-isolation cases here are the security-relevant contract: a
``RUN``-scoped memory must never surface outside its own run, and a
``USER``-scoped memory must never cross tenants, mirroring the pure
``visible_to`` checks in ``test_ports_memory.py``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.control_plane.engine import (
    create_control_plane_engine,
    create_session_factory,
)
from harborrag_adapters.repositories.database.control_plane.memory import SqlMemoryRepository
from harborrag_adapters.repositories.database.control_plane.migrations import run_migrations
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryScope,
    MemoryType,
    new_memory_id,
)

pytestmark = pytest.mark.integration


def _memory(owner: MemoryOwner, scope: MemoryScope, content: str, **kwargs: object) -> Memory:
    now = datetime.now(UTC)
    return Memory(
        memory_id=new_memory_id(),
        scope=scope,
        memory_type=kwargs.get("memory_type", MemoryType.FACT),  # type: ignore[arg-type]
        owner=owner,
        content=content,
        importance=kwargs.get("importance", 0.5),  # type: ignore[arg-type]
        created_at=now,
        updated_at=now,
        expires_at=kwargs.get("expires_at"),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_memory_save_and_get_round_trip(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        repo = SqlMemoryRepository(sessions)
        owner = MemoryOwner(tenant_id="ACME", principal_id="user-1")
        memory = _memory(owner, MemoryScope.USER, "prefers concise answers")

        await repo.save(memory)
        loaded = await repo.get(owner, memory.memory_id)

        assert loaded is not None
        assert loaded.content == "prefers concise answers"
        assert loaded.scope is MemoryScope.USER
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_memory_save_upserts_by_memory_id(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        repo = SqlMemoryRepository(sessions)
        owner = MemoryOwner(tenant_id="ACME", principal_id="user-1")
        memory = _memory(owner, MemoryScope.USER, "v1")

        await repo.save(memory)
        updated = replace(memory, content="v2")
        await repo.save(updated)

        loaded = await repo.get(owner, memory.memory_id)
        assert loaded is not None
        assert loaded.content == "v2"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_memory_save_rejects_cross_owner_id_collision(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        repo = SqlMemoryRepository(sessions)
        owner = MemoryOwner(tenant_id="ACME", principal_id="user-1")
        victim = _memory(owner, MemoryScope.USER, "victim")
        await repo.save(victim)
        attacker = replace(
            victim,
            owner=MemoryOwner(tenant_id="OTHER", principal_id="user-2"),
            content="replaced",
        )

        with pytest.raises(HarborConflictError, match="different owner or scope"):
            await repo.save(attacker)

        assert (await repo.get(owner, victim.memory_id)).content == "victim"  # type: ignore[union-attr]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_expired_memories_are_not_readable_or_searchable(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        repo = SqlMemoryRepository(sessions)
        owner = MemoryOwner(tenant_id="ACME", principal_id="user-1")
        now = datetime.now(UTC)
        memory = replace(
            _memory(owner, MemoryScope.USER, "expired", expires_at=now + timedelta(seconds=1)),
            created_at=now - timedelta(seconds=2),
            updated_at=now - timedelta(seconds=2),
            expires_at=now - timedelta(seconds=1),
        )
        await repo.save(memory)

        assert await repo.get(owner, memory.memory_id) is None
        assert await repo.search(MemoryQuery(owner=owner)) == ()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_run_scoped_memory_does_not_leak_across_runs(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        repo = SqlMemoryRepository(sessions)
        owner = MemoryOwner(
            tenant_id="ACME", principal_id="user-1", session_id="session-1", run_id="run-1"
        )
        memory = _memory(owner, MemoryScope.RUN, "scratch note for run-1")
        await repo.save(memory)

        other_run = MemoryOwner(
            tenant_id="ACME", principal_id="user-1", session_id="session-1", run_id="run-2"
        )
        assert await repo.get(other_run, memory.memory_id) is None
        results = await repo.search(MemoryQuery(owner=other_run, scopes=(MemoryScope.RUN,)))
        assert results == ()

        results = await repo.search(MemoryQuery(owner=owner, scopes=(MemoryScope.RUN,)))
        assert [item.memory_id for item in results] == [memory.memory_id]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_user_scoped_memory_does_not_cross_tenants(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        repo = SqlMemoryRepository(sessions)
        owner = MemoryOwner(tenant_id="ACME", principal_id="user-1")
        memory = _memory(owner, MemoryScope.USER, "acme fact")
        await repo.save(memory)

        other_tenant_same_user = MemoryOwner(tenant_id="OTHER", principal_id="user-1")
        assert await repo.get(other_tenant_same_user, memory.memory_id) is None
        results = await repo.search(
            MemoryQuery(owner=other_tenant_same_user, scopes=(MemoryScope.USER,))
        )
        assert results == ()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_tenant_scoped_memory_is_visible_to_any_user_in_the_tenant(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        repo = SqlMemoryRepository(sessions)
        writer = MemoryOwner(tenant_id="ACME", principal_id="user-1")
        memory = _memory(writer, MemoryScope.TENANT, "tenant-wide policy note")
        await repo.save(memory)

        other_user = MemoryOwner(tenant_id="ACME", principal_id="user-2")
        results = await repo.search(MemoryQuery(owner=other_user, scopes=(MemoryScope.TENANT,)))
        assert [item.memory_id for item in results] == [memory.memory_id]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_search_filters_by_memory_type(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        repo = SqlMemoryRepository(sessions)
        owner = MemoryOwner(tenant_id="ACME", principal_id="user-1")
        fact = _memory(owner, MemoryScope.USER, "a fact", memory_type=MemoryType.FACT)
        preference = _memory(
            owner, MemoryScope.USER, "a preference", memory_type=MemoryType.PREFERENCE
        )
        await repo.save(fact)
        await repo.save(preference)

        results = await repo.search(
            MemoryQuery(owner=owner, scopes=(MemoryScope.USER,), memory_types=(MemoryType.FACT,))
        )
        assert [item.memory_id for item in results] == [fact.memory_id]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.whitebox
async def test_delete_is_scoped_to_the_caller(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path}/control.db"
    run_migrations(dsn)
    engine = create_control_plane_engine(dsn)
    sessions = create_session_factory(engine)
    try:
        repo = SqlMemoryRepository(sessions)
        owner = MemoryOwner(tenant_id="ACME", principal_id="user-1")
        memory = _memory(owner, MemoryScope.USER, "acme fact")
        await repo.save(memory)

        other_tenant = MemoryOwner(tenant_id="OTHER", principal_id="user-1")
        await repo.delete(other_tenant, memory.memory_id)
        assert await repo.get(owner, memory.memory_id) is not None

        await repo.delete(owner, memory.memory_id)
        assert await repo.get(owner, memory.memory_id) is None
    finally:
        await engine.dispose()
