from __future__ import annotations

import asyncio
import os

from bootstrap import (
    dependency_available,
    env,
    env_bool,
    env_int,
    load_env,
    probe_suffix,
    require_healthy,
    safe_error,
)
from harborrag_adapters.repositories.vector.qdrant import (
    QdrantVectorConfig,
    QdrantVectorRepository,
)
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import (
    VectorCollectionSpec,
    VectorPoint,
    VectorSearchQuery,
)


def _qdrant_url() -> str:
    configured = os.getenv("HARBOR_SMOKE_QDRANT_URL")
    if configured and configured.strip():
        return configured.strip()
    return f"http://127.0.0.1:{env_int('QDRANT_HTTP_PORT', 6333)}"


async def _run() -> tuple[str, str]:
    suffix = probe_suffix()
    collection = env("HARBOR_SMOKE_QDRANT_COLLECTION", "repository_probe")
    point_id = f"point-{suffix}"
    context = StorageOperationContext(tenant_id=f"smoke-{suffix}")
    backend = QdrantVectorRepository(
        QdrantVectorConfig(
            instance_name="smoke",
            url=_qdrant_url(),
            prefer_grpc=env_bool("HARBOR_SMOKE_QDRANT_PREFER_GRPC", False),
            collection_prefix=env("HARBOR_SMOKE_QDRANT_PREFIX", "harborrag_smoke_"),
        )
    )
    async with backend:
        require_healthy(await backend.health())
        await backend.ensure_collection(
            VectorCollectionSpec(name=collection, dimension=3),
            context=context,
        )
        await backend.upsert(
            collection,
            [
                VectorPoint(
                    id=point_id,
                    tenant_id=context.tenant_id,
                    vector=[1.0, 0.0, 0.0],
                    payload={"smoke_test": True},
                )
            ],
            context=context,
        )
        try:
            loaded = await backend.get(collection, [point_id], context=context)
            if [point.id for point in loaded] != [point_id]:
                raise AssertionError("Qdrant point did not round-trip")
            results = await backend.search(
                VectorSearchQuery(
                    collection=collection,
                    vector=[1.0, 0.0, 0.0],
                    top_k=1,
                ),
                context=context,
            )
            if not results or results[0].id != point_id:
                raise AssertionError("Qdrant search did not return the smoke point")
        finally:
            await backend.delete(collection, [point_id], context=context)
            await backend.delete_collection(collection, context=context)
    return collection, point_id


def main() -> int:
    load_env()
    if not dependency_available(
        "qdrant_client",
        'Install it with: uv pip install -e "packages/harborrag-adapters[qdrant]"',
    ):
        return 2
    try:
        collection, point_id = asyncio.run(_run())
    except Exception as exc:
        print(f"Qdrant smoke failed: {safe_error(exc)}")
        return 1
    print(f"Qdrant smoke passed: searched and deleted point={point_id!r} in {collection!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
