"""Independent final serving checks for graph builds and derived-view lineage."""

from __future__ import annotations

import asyncio

from harborrag_core.indexing import VectorSearchResult
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.search import TopologyEvidence, TopologySearchPort


async def eligible_semantic_candidates(
    repository: TopologySearchPort,
    candidates: tuple[VectorSearchResult, ...],
    evidence: dict[str, TopologyEvidence],
    context: StorageOperationContext,
) -> tuple[VectorSearchResult, ...]:
    builds = tuple(sorted({build for item in evidence.values() for build in item.build_ids}))
    artifacts = tuple(
        sorted({artifact for item in evidence.values() for artifact in item.derived_artifact_ids})
    )
    async with asyncio.timeout(1):
        eligible_builds = await repository.eligible_build_ids(
            str(context.tenant_id), builds, access=context.access
        )
        eligible_artifacts = (
            await repository.eligible_artifact_ids(
                str(context.tenant_id), artifacts, access=context.access
            )
            if artifacts
            else set()
        )
    return tuple(
        item
        for item in candidates
        if (support := evidence[str(item.payload["chunk_id"])])
        and set(support.build_ids) <= eligible_builds
        and set(support.derived_artifact_ids) <= eligible_artifacts
    )
