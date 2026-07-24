from __future__ import annotations

import asyncio
import os
from datetime import timedelta

from bootstrap import (
    dependency_available,
    env_int,
    load_env,
    probe_suffix,
    require_healthy,
    safe_error,
)

from harborrag_adapters.repositories.cache.redis import (
    RedisCacheBackend,
    RedisCacheConfig,
)
from harborrag_core.schemas.storage import StorageOperationContext


def _redis_url() -> str:
    configured = os.getenv("HARBOR_SMOKE_REDIS_URL")
    if configured and configured.strip():
        return configured.strip()
    return f"redis://127.0.0.1:{env_int('REDIS_PORT', 6380)}/15"


async def _run() -> str:
    suffix = probe_suffix()
    context = StorageOperationContext(tenant_id=f"smoke-{suffix}")
    key = f"probe-{suffix}"
    backend = RedisCacheBackend(
        RedisCacheConfig(
            instance_name="smoke",
            url=_redis_url(),
            key_prefix="harborrag:smoke",
        )
    )
    async with backend:
        require_healthy(await backend.health())
        await backend.cache.set(
            key,
            {"value": 1},
            ttl=timedelta(minutes=1),
            tags={"smoke"},
            context=context,
        )
        try:
            if await backend.cache.get(key, context=context) != {"value": 1}:
                raise AssertionError("Redis value did not round-trip")
            replaced = await backend.cache.compare_and_set(
                key,
                {"value": 1},
                {"value": 2},
                ttl=timedelta(minutes=1),
                context=context,
            )
            if not replaced or await backend.cache.get(key, context=context) != {"value": 2}:
                raise AssertionError("Redis compare-and-set did not round-trip")
        finally:
            await backend.cache.delete(key, context=context)
    return key


def main() -> int:
    load_env()
    if not dependency_available(
        "redis",
        'Install it with: uv pip install -e "packages/harborrag-adapters[redis]"',
    ):
        return 2
    try:
        key = asyncio.run(_run())
    except Exception as exc:
        print(f"Redis smoke failed: {safe_error(exc)}")
        return 1
    print(f"Redis smoke passed: set, replaced, read, and deleted key={key!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
