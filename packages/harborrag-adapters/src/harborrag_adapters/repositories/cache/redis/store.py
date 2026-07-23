from __future__ import annotations

import json
import math
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from harborrag_core.schemas.storage import StorageOperationContext

from harborrag_adapters.repositories.cache.base import HarborCacheStore
from harborrag_adapters.repositories.telemetry import traced_repository_operation

try:
    from redis.exceptions import ResponseError, WatchError
except ImportError:  # pragma: no cover - optional dependency
    ResponseError = RuntimeError
    WatchError = RuntimeError

if TYPE_CHECKING:
    from harborrag_adapters.repositories.cache.redis.repository import (
        RedisCacheBackend,
    )


class RedisCacheStore(HarborCacheStore):
    """Provides tenant-aware Redis caching, tags, counters, and atomic replacement."""

    def __init__(self, repositories: RedisCacheBackend) -> None:
        self._repositories = repositories
        self._telemetry = repositories._telemetry

    @traced_repository_operation("get")
    async def get(self, key: str, *, context: StorageOperationContext) -> Any | None:
        raw = await self._repositories.client.get(self._repositories.value_key(context, key))
        return json.loads(raw) if raw is not None else None

    @traced_repository_operation("get_many")
    async def get_many(
        self,
        keys: list[str],
        *,
        context: StorageOperationContext,
    ) -> dict[str, Any]:
        if not keys:
            return {}
        raw_values = await self._repositories.client.mget(
            [self._repositories.value_key(context, key) for key in keys]
        )
        return {
            key: json.loads(raw)
            for key, raw in zip(keys, raw_values, strict=True)
            if raw is not None
        }

    @traced_repository_operation("set")
    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl: timedelta | None,
        tags: set[str] | None,
        context: StorageOperationContext,
    ) -> None:
        value_key = self._repositories.value_key(context, key)
        tags_key = self._repositories.tags_key(context, key)
        ttl_ms = self._ttl_milliseconds(ttl)
        while True:
            async with self._repositories.client.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(tags_key)
                    old_tags = await pipe.smembers(tags_key)
                    pipe.multi()
                    for tag in old_tags:
                        pipe.srem(self._repositories.tag_key(context, tag), value_key)
                    pipe.delete(tags_key)
                    if ttl_ms == 0:
                        pipe.delete(value_key)
                    else:
                        pipe.set(
                            value_key,
                            json.dumps(value, separators=(",", ":")),
                            px=ttl_ms,
                        )
                        for tag in tags or set():
                            pipe.sadd(self._repositories.tag_key(context, tag), value_key)
                            pipe.sadd(tags_key, tag)
                        if ttl_ms is not None and tags:
                            pipe.pexpire(tags_key, ttl_ms)
                    await pipe.execute()
                    return
                except WatchError:
                    continue

    @traced_repository_operation("compare_and_set")
    async def compare_and_set(
        self,
        key: str,
        expected: Any,
        value: Any,
        *,
        ttl: timedelta | None,
        context: StorageOperationContext,
    ) -> bool:
        value_key = self._repositories.value_key(context, key)
        tags_key = self._repositories.tags_key(context, key)
        ttl_ms = self._ttl_milliseconds(ttl)
        async with self._repositories.client.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(value_key, tags_key)
                raw = await pipe.get(value_key)
                current = json.loads(raw) if raw is not None else None
                if current != expected:
                    return False
                old_tags = await pipe.smembers(tags_key)
                pipe.multi()
                if ttl_ms == 0:
                    for tag in old_tags:
                        pipe.srem(self._repositories.tag_key(context, tag), value_key)
                    pipe.delete(tags_key)
                    pipe.delete(value_key)
                else:
                    pipe.set(
                        value_key,
                        json.dumps(value, separators=(",", ":")),
                        px=ttl_ms,
                    )
                    if old_tags:
                        if ttl_ms is None:
                            pipe.persist(tags_key)
                        else:
                            pipe.pexpire(tags_key, ttl_ms)
                await pipe.execute()
                return True
            except WatchError:
                return False

    @traced_repository_operation("increment")
    async def increment(
        self,
        key: str,
        amount: int,
        *,
        ttl: timedelta | None,
        context: StorageOperationContext,
    ) -> int:
        value_key = self._repositories.value_key(context, key)
        ttl_ms = self._ttl_milliseconds(ttl)
        try:
            async with self._repositories.client.pipeline(transaction=True) as pipe:
                pipe.incrby(value_key, amount)
                if ttl_ms is not None:
                    pipe.pexpire(value_key, ttl_ms)
                result = await pipe.execute()
        except ResponseError as exc:
            raise TypeError("cache increment requires an integer value") from exc
        return int(result[0])

    @traced_repository_operation("delete")
    async def delete(self, key: str, *, context: StorageOperationContext) -> bool:
        value_key = self._repositories.value_key(context, key)
        tags_key = self._repositories.tags_key(context, key)
        tags = await self._repositories.client.smembers(tags_key)
        async with self._repositories.client.pipeline(transaction=True) as pipe:
            for tag in tags:
                pipe.srem(self._repositories.tag_key(context, tag), value_key)
            pipe.delete(tags_key)
            pipe.delete(value_key)
            result = await pipe.execute()
        return bool(result[-1])

    @traced_repository_operation("invalidate_tag")
    async def invalidate_tag(
        self,
        tag: str,
        *,
        context: StorageOperationContext,
    ) -> int:
        tag_key = self._repositories.tag_key(context, tag)
        while True:
            async with self._repositories.client.pipeline(transaction=True) as pipe:
                try:
                    # WATCH the tag membership and every tagged value's own tag
                    # index before reading them, so a concurrent set()/
                    # compare_and_set() that re-tags one of these values
                    # between our read and EXEC aborts this transaction
                    # (WatchError) instead of silently invalidating against
                    # stale membership data.
                    await pipe.watch(tag_key)
                    value_keys = list(await pipe.smembers(tag_key))
                    if not value_keys:
                        return 0
                    tags_keys = {
                        value_key: self._repositories.tags_key_for_value_key(value_key)
                        for value_key in value_keys
                    }
                    await pipe.watch(*tags_keys.values())
                    # A single per-value SMEMBERS round trip inside the WATCH
                    # window, repeated for every tagged value, dominates
                    # latency for large tags. Reading is safe to batch into
                    # one pipelined round trip (no MULTI/EXEC needed here --
                    # WATCH already started tracking these keys for the
                    # optimistic-lock check below) instead of issuing them
                    # sequentially one at a time.
                    async with self._repositories.client.pipeline(transaction=False) as batch:
                        for tags_key in tags_keys.values():
                            batch.smembers(tags_key)
                        raw_memberships = await batch.execute()
                    memberships: dict[str, set[str]] = {
                        value_key: set(raw)
                        for value_key, raw in zip(tags_keys.keys(), raw_memberships, strict=True)
                    }
                    pipe.multi()
                    value_delete_indexes: list[int] = []
                    command_index = 0
                    for value_key, tags in memberships.items():
                        for item_tag in tags:
                            if item_tag == tag:
                                # tag_key (this exact index) is deleted
                                # unconditionally below, so removing
                                # value_key from it here first is wasted work.
                                continue
                            pipe.srem(self._repositories.tag_key(context, item_tag), value_key)
                            command_index += 1
                        pipe.delete(tags_keys[value_key])
                        command_index += 1
                        pipe.delete(value_key)
                        value_delete_indexes.append(command_index)
                        command_index += 1
                    pipe.delete(tag_key)
                    results = await pipe.execute()
                    # Members may have already expired naturally; only count
                    # keys actually deleted here.
                    return sum(int(results[index]) for index in value_delete_indexes)
                except WatchError:
                    continue

    @staticmethod
    def _ttl_milliseconds(ttl: timedelta | None) -> int | None:
        if ttl is None:
            return None
        milliseconds = ttl.total_seconds() * 1000
        return 0 if milliseconds <= 0 else max(1, math.ceil(milliseconds))
