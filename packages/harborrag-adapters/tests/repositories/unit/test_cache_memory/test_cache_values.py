from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from harborrag_adapters.repositories.cache.memory.backend import MemoryCacheBackend
from harborrag_core.schemas.storage import HealthStatus

from .conftest import make_context


@pytest.mark.asyncio
async def test_set_get_delete_round_trip() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        await backend.cache.set("key", {"a": 1}, ttl=None, tags=None, context=context)
        assert await backend.cache.get("key", context=context) == {"a": 1}
        assert await backend.cache.delete("key", context=context) is True
        assert await backend.cache.get("key", context=context) is None


@pytest.mark.asyncio
async def test_ttl_expires_value() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        await backend.cache.set(
            "key", "value", ttl=timedelta(seconds=-1), tags=None, context=context
        )
        assert await backend.cache.get("key", context=context) is None


@pytest.mark.asyncio
async def test_compare_and_set_only_replaces_matching_value() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        await backend.cache.set("key", "old", ttl=None, tags=None, context=context)
        assert (
            await backend.cache.compare_and_set("key", "wrong", "new", ttl=None, context=context)
            is False
        )
        assert (
            await backend.cache.compare_and_set("key", "old", "new", ttl=None, context=context)
            is True
        )
        assert await backend.cache.get("key", context=context) == "new"


@pytest.mark.asyncio
async def test_compare_and_set_preserves_tag_membership() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        await backend.cache.set("key", "old", ttl=None, tags={"grp"}, context=context)
        assert await backend.cache.compare_and_set("key", "old", "new", ttl=None, context=context)

        assert await backend.cache.invalidate_tag("grp", context=context) == 1
        assert await backend.cache.get("key", context=context) is None


@pytest.mark.asyncio
async def test_increment_requires_integer_value() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        assert await backend.cache.increment("counter", 5, ttl=None, context=context) == 5
        assert await backend.cache.increment("counter", 2, ttl=None, context=context) == 7
        await backend.cache.set("text", "not-a-number", ttl=None, tags=None, context=context)
        with pytest.raises(TypeError):
            await backend.cache.increment("text", 1, ttl=None, context=context)


@pytest.mark.asyncio
async def test_invalidate_tag_removes_only_live_entries() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        await backend.cache.set("a", 1, ttl=None, tags={"grp"}, context=context)
        await backend.cache.set("b", 2, ttl=None, tags={"grp"}, context=context)
        # "a" is overwritten without the tag, so it should no longer be affected.
        await backend.cache.set("a", 1, ttl=None, tags=None, context=context)
        removed = await backend.cache.invalidate_tag("grp", context=context)
        assert removed == 1
        assert await backend.cache.get("a", context=context) == 1
        assert await backend.cache.get("b", context=context) is None


@pytest.mark.asyncio
async def test_keys_with_colons_do_not_collide() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        await backend.cache.set("a:b", "colon", ttl=None, tags=None, context=context)
        await backend.cache.set("a_b", "underscore", ttl=None, tags=None, context=context)
        assert await backend.cache.get("a:b", context=context) == "colon"
        assert await backend.cache.get("a_b", context=context) == "underscore"


@pytest.mark.asyncio
async def test_tenants_do_not_share_cache_entries() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        await backend.cache.set(
            "shared-key",
            "tenant-a-value",
            ttl=None,
            tags=None,
            context=make_context("tenant-a"),
        )
        assert await backend.cache.get("shared-key", context=make_context("tenant-b")) is None


@pytest.mark.asyncio
async def test_get_many_returns_only_present_values() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        await backend.cache.set("a", 1, ttl=None, tags=None, context=context)
        await backend.cache.set("b", 2, ttl=None, tags=None, context=context)
        result = await backend.cache.get_many(["a", "b", "missing"], context=context)
        assert result == {"a": 1, "b": 2}


@pytest.mark.asyncio
async def test_compare_and_set_with_no_prior_entry_creates_one() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        created = await backend.cache.compare_and_set(
            "fresh", None, "value", ttl=None, context=context
        )
        assert created is True
        assert await backend.cache.get("fresh", context=context) == "value"


@pytest.mark.asyncio
async def test_compare_and_set_with_non_positive_ttl_removes_without_replacing() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        await backend.cache.set("key", "old", ttl=None, tags=None, context=context)
        replaced = await backend.cache.compare_and_set(
            "key", "old", "new", ttl=timedelta(seconds=-1), context=context
        )
        assert replaced is True
        assert await backend.cache.get("key", context=context) is None


@pytest.mark.asyncio
async def test_increment_with_non_positive_ttl_removes_existing_entry() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        await backend.cache.increment("counter", 5, ttl=None, context=context)
        result = await backend.cache.increment(
            "counter", 1, ttl=timedelta(seconds=-1), context=context
        )
        assert result == 6
        assert await backend.cache.get("counter", context=context) is None


@pytest.mark.asyncio
async def test_increment_with_non_positive_ttl_on_a_missing_key_creates_nothing() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        result = await backend.cache.increment(
            "never-set", 3, ttl=timedelta(seconds=-1), context=context
        )
        assert result == 3
        assert await backend.cache.get("never-set", context=context) is None


@pytest.mark.asyncio
async def test_delete_returns_false_for_a_missing_key() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        assert await backend.cache.delete("does-not-exist", context=context) is False


@pytest.mark.asyncio
async def test_invalidate_tag_skips_members_whose_entry_was_already_removed() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        await backend.cache.set("a", 1, ttl=None, tags={"grp"}, context=context)
        await backend.cache.set("b", 2, ttl=None, tags={"grp"}, context=context)
        # Simulate a stale tag pointer left over after out-of-band removal: the tag
        # index still references "a" even though its entry is gone.
        scoped_a = backend._state.key(context, "a")
        del backend._state.entries[scoped_a]

        removed = await backend.cache.invalidate_tag("grp", context=context)

        assert removed == 1
        assert await backend.cache.get("b", context=context) is None


@pytest.mark.asyncio
async def test_get_expires_entry_naturally_after_ttl_elapses() -> None:
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        await backend.cache.set(
            "short-lived",
            "value",
            ttl=timedelta(milliseconds=1),
            tags=None,
            context=context,
        )
        await asyncio.sleep(0.05)
        assert await backend.cache.get("short-lived", context=context) is None


@pytest.mark.asyncio
async def test_live_entry_pops_expired_entry_directly_when_called_without_context() -> None:
    """_live_entry(scoped) is defensively callable without a context (default None);
    exercise that private branch directly since no public call site takes it."""
    backend = MemoryCacheBackend()
    async with backend:
        context = make_context()
        await backend.cache.set("stale", "value", ttl=None, tags=None, context=context)
        scoped = backend._state.key(context, "stale")
        # Force the entry to look expired without going through set()'s own ttl<=0
        # early-return, so _live_entry has to discover and remove it itself.
        entry = backend._state.entries[scoped]
        backend._state.entries[scoped] = entry.model_copy(
            update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
        )

        assert backend.cache._live_entry(scoped) is None
        assert scoped not in backend._state.entries


@pytest.mark.asyncio
async def test_health_reports_unknown_before_connect_and_healthy_after() -> None:
    backend = MemoryCacheBackend()
    disconnected = await backend.health()
    assert disconnected.status == HealthStatus.UNKNOWN

    async with backend:
        context = make_context()
        await backend.cache.set("a", 1, ttl=None, tags=None, context=context)
        connected = await backend.health()
        assert connected.status == HealthStatus.HEALTHY
        assert connected.details["entries"] == 1
