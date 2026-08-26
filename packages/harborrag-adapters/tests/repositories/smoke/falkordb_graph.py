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
from pydantic import SecretStr

from harborrag_adapters.repositories.graph.falkordb import (
    FalkorDBGraphConfig,
    FalkorDBGraphRepository,
)
from harborrag_core.schemas.graph import GraphEdge, GraphExpansionQuery, GraphNode
from harborrag_core.schemas.storage import StorageOperationContext


async def _run() -> tuple[str, str]:
    suffix = probe_suffix()
    source_id = f"source-{suffix}"
    target_id = f"target-{suffix}"
    edge_id = f"edge-{suffix}"
    context = StorageOperationContext.system(tenant_id=f"smoke-{suffix}")
    password = os.getenv("FALKORDB_PASSWORD")
    backend = FalkorDBGraphRepository(
        FalkorDBGraphConfig(
            instance_name="smoke",
            host=env("FALKORDB_HOST", "127.0.0.1"),
            port=env_int("FALKORDB_PORT", 6379),
            username=os.getenv("FALKORDB_USERNAME"),
            password=SecretStr(password) if password else None,
            graph_name=env("HARBOR_SMOKE_FALKORDB_GRAPH", "harborrag_smoke"),
            ssl=env_bool("FALKORDB_SSL", False),
        )
    )
    async with backend:
        require_healthy(await backend.health())
        await backend.upsert_nodes(
            [
                GraphNode(
                    id=source_id,
                    tenant_id=context.tenant_id,
                    labels={"SmokeProbe"},
                    properties={"side": "source"},
                ),
                GraphNode(
                    id=target_id,
                    tenant_id=context.tenant_id,
                    labels={"SmokeProbe"},
                    properties={"side": "target"},
                ),
            ],
            context=context,
        )
        try:
            await backend.upsert_edges(
                [
                    GraphEdge(
                        id=edge_id,
                        tenant_id=context.tenant_id,
                        source_id=source_id,
                        target_id=target_id,
                        relationship_type="SMOKE_LINK",
                    )
                ],
                context=context,
            )
            loaded = await backend.get_nodes([source_id, target_id], context=context)
            if {str(node.id) for node in loaded} != {source_id, target_id}:
                raise AssertionError("FalkorDB nodes did not round-trip")
            subgraph = await backend.expand(
                GraphExpansionQuery(
                    start_nodes=[source_id],
                    relationship_types=["SMOKE_LINK"],
                    max_depth=1,
                    max_nodes=10,
                    direction="outgoing",
                ),
                context=context,
            )
            if target_id not in {str(node.id) for node in subgraph.nodes}:
                raise AssertionError("FalkorDB expansion did not reach the target node")
        finally:
            await backend.delete_nodes([source_id, target_id], context=context)
    return source_id, edge_id


def main() -> int:
    load_env()
    if not dependency_available(
        "falkordb",
        'Install it with: uv pip install -e "packages/harborrag-adapters[falkordb]"',
    ):
        return 2
    try:
        source_id, edge_id = asyncio.run(_run())
    except Exception as exc:
        print(f"FalkorDB smoke failed: {safe_error(exc)}")
        return 1
    print(f"FalkorDB smoke passed: expanded and deleted source={source_id!r}, edge={edge_id!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
