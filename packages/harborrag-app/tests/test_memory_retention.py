"""Memories past their scope's retention are erased, canonical row and vector alike.

Retention is opt-in: with no scope enabled nothing is ever deleted.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
from app_test_memory import FakeMemoryIndex, FakeMemoryStore, memory

from harborrag_app.workflow_control.composition.service import AppService
from harborrag_app.workflow_control.memory import MemoryAdministrationService
from harborrag_core.base import utc_now
from harborrag_core.domain.identity import DEFAULT_USER
from harborrag_core.ports.memory import MemoryOwner, MemoryQuery, MemoryScope
from harborrag_core.testing.control_plane_fakes import FakePendingEffectRepository
from harborrag_runtime.composition import CompositionRoot
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import InMemoryConversationMemory


def _admin(store: FakeMemoryStore, index: FakeMemoryIndex) -> MemoryAdministrationService:
    return MemoryAdministrationService(
        conversations=InMemoryConversationMemory(),
        memories=store,
        index=index,
        pending_effects=FakePendingEffectRepository(),
    )


@pytest.mark.asyncio
async def test_purge_expired_erases_only_memories_past_their_scope_retention() -> None:
    store, index = FakeMemoryStore(), FakeMemoryIndex()
    now = utc_now()
    old = now - timedelta(days=400)
    await store.save(memory("user-old", updated_at=old))
    await store.save(memory("user-fresh"))
    await store.save(memory("tenant-old", scope=MemoryScope.TENANT, updated_at=old))
    await store.save(memory("session-old", scope=MemoryScope.SESSION, updated_at=old))
    admin = _admin(store, index)
    cutoffs = {MemoryScope.USER: now - timedelta(days=365)}

    assert await admin.purge_expired(cutoffs, now=now) == 1

    assert sorted(store.rows) == ["session-old", "tenant-old", "user-fresh"]
    assert index.deleted == ["user-old"]
    assert await admin.purge_expired(cutoffs, now=now) == 0


@pytest.mark.asyncio
async def test_without_cutoffs_the_purge_deletes_nothing_even_with_ancient_rows() -> None:
    store, index = FakeMemoryStore(), FakeMemoryIndex()
    ancient = utc_now() - timedelta(days=5000)
    await store.save(memory("user-ancient", updated_at=ancient))
    await store.save(memory("session-ancient", scope=MemoryScope.SESSION, updated_at=ancient))

    assert await _admin(store, index).purge_expired({}, now=utc_now()) == 0

    assert sorted(store.rows) == ["session-ancient", "user-ancient"]
    assert index.deleted == []


@pytest.mark.asyncio
async def test_the_fake_store_validates_the_listing_limit_like_sql() -> None:
    with pytest.raises(ValueError, match="limit"):
        await FakeMemoryStore().list_expired(cutoffs={}, now=utc_now(), limit=0)


@pytest.mark.asyncio
async def test_without_a_memory_store_retention_is_a_no_op() -> None:
    admin = MemoryAdministrationService(conversations=InMemoryConversationMemory())

    assert await admin.purge_expired({MemoryScope.USER: utc_now()}, now=utc_now()) == 0


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [key for key in os.environ if key.startswith("HARBORRAG_")]:
        monkeypatch.delenv(name, raising=False)


async def _remaining(composition: CompositionRoot) -> list[str]:
    cp = composition.control_plane
    assert cp is not None and cp.memories is not None
    owner = MemoryOwner(tenant_id="ACME", user_id=DEFAULT_USER)
    query = MemoryQuery(owner=owner, include_invalid=True, stored_by_owner=True)
    return sorted(row.memory_id for row in await cp.memories.search(query))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retention_runs_under_a_single_lease_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_env(monkeypatch)
    settings = RuntimeSettings(
        env="dev",
        control_db_url=f"sqlite+aiosqlite:///{tmp_path}/control.db",
        memory_retention_days_user=30,
    )
    composition = CompositionRoot.production(settings)
    try:
        cp = composition.control_plane
        assert cp is not None and cp.memories is not None
        await cp.memories.save(
            memory("aged", tenant_id="ACME", updated_at=utc_now() - timedelta(days=60))
        )
        await cp.memories.save(memory("fresh", tenant_id="ACME"))
        holder, contender = AppService(composition, settings), AppService(composition, settings)

        assert await holder.purge_expired_memories() == 1
        assert await contender.purge_expired_memories() == 0

        assert await _remaining(composition) == ["fresh"]
    finally:
        await composition.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_default_settings_never_purge_even_ancient_memories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_env(monkeypatch)
    settings = RuntimeSettings(
        env="dev", control_db_url=f"sqlite+aiosqlite:///{tmp_path}/control.db"
    )
    composition = CompositionRoot.production(settings)
    try:
        cp = composition.control_plane
        assert cp is not None and cp.memories is not None
        await cp.memories.save(
            memory("ancient", tenant_id="ACME", updated_at=utc_now() - timedelta(days=5000))
        )

        assert await AppService(composition, settings).purge_expired_memories() == 0

        assert await _remaining(composition) == ["ancient"]
    finally:
        await composition.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_enabled_scope_purges_only_that_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_env(monkeypatch)
    settings = RuntimeSettings(
        env="dev",
        control_db_url=f"sqlite+aiosqlite:///{tmp_path}/control.db",
        memory_retention_days_session=30,
    )
    composition = CompositionRoot.production(settings)
    try:
        cp = composition.control_plane
        assert cp is not None and cp.memories is not None
        old = utc_now() - timedelta(days=400)
        await cp.memories.save(memory("user-old", tenant_id="ACME", updated_at=old))
        await cp.memories.save(
            memory(
                "session-old",
                tenant_id="ACME",
                scope=MemoryScope.SESSION,
                session_id="session-a",
                updated_at=old,
            )
        )

        assert await AppService(composition, settings).purge_expired_memories() == 1

        assert await _remaining(composition) == ["user-old"]
    finally:
        await composition.aclose()
