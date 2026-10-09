"""Working-memory keys must be injective over the owner they isolate."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from harborrag_adapters.repositories.memory.redis_working_memory import RedisWorkingMemoryStore
from harborrag_core.ports.memory import MemoryOwner
from harborrag_core.storage import StorageOperationContext

pytestmark = pytest.mark.unit


class _RecordingCache:
    """Just the HarborCacheStore surface RedisWorkingMemoryStore uses."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], Any] = {}

    async def get(self, key: str, *, context: StorageOperationContext) -> Any | None:
        return self.values.get((str(context.tenant_id), key))

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl: timedelta | None,
        tags: set[str] | None,
        context: StorageOperationContext,
    ) -> None:
        del ttl, tags
        self.values[(str(context.tenant_id), key)] = value

    async def delete(self, key: str, *, context: StorageOperationContext) -> bool:
        return self.values.pop((str(context.tenant_id), key), None) is not None


def _store() -> tuple[RedisWorkingMemoryStore, _RecordingCache]:
    cache = _RecordingCache()
    return RedisWorkingMemoryStore(cache), cache  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_separator_inside_a_part_cannot_collide_with_a_boundary() -> None:
    store, _ = _store()
    first = MemoryOwner(tenant_id="t", principal_id="a:b", session_id="c", run_id="r")
    second = MemoryOwner(tenant_id="t", principal_id="a", session_id="b:c", run_id="r")

    await store.put(first, {"who": "first"}, ttl_seconds=60)
    await store.put(second, {"who": "second"}, ttl_seconds=60)

    assert await store.get(first) == {"who": "first"}
    assert await store.get(second) == {"who": "second"}


@pytest.mark.asyncio
async def test_a_missing_field_is_not_the_string_none() -> None:
    store, cache = _store()
    absent = MemoryOwner(tenant_id="t", principal_id="p", session_id="s")
    literal = MemoryOwner(tenant_id="t", principal_id="p", session_id="s", run_id="None")

    await store.put(absent, {"run": "absent"}, ttl_seconds=60)
    await store.put(literal, {"run": "literal"}, ttl_seconds=60)

    assert len(cache.values) == 2
    assert await store.get(absent) == {"run": "absent"}


@pytest.mark.asyncio
async def test_different_end_users_behind_one_credential_do_not_share_state() -> None:
    store, _ = _store()
    alice = MemoryOwner(
        tenant_id="t", user_id="alice", principal_id="svc", session_id="s", run_id="r"
    )
    bob = MemoryOwner(tenant_id="t", user_id="bob", principal_id="svc", session_id="s", run_id="r")

    await store.put(alice, {"who": "alice"}, ttl_seconds=60)

    assert await store.get(bob) is None


@pytest.mark.asyncio
async def test_keys_carry_a_version_segment() -> None:
    store, cache = _store()
    await store.put(
        MemoryOwner(tenant_id="t", principal_id="p", session_id="s", run_id="r"), {}, ttl_seconds=60
    )

    ((_, key),) = cache.values
    assert key.startswith("working_memory:v2:")
