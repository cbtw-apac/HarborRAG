from __future__ import annotations

import asyncio
from dataclasses import dataclass

from bootstrap import (
    SmokeConfigurationError,
    SmokeNotConfigured,
    dependency_available,
    load_env,
    require_healthy,
    safe_error,
)


@dataclass(frozen=True, slots=True)
class SmokeSummary:
    """Safe counts produced by one real indexing smoke run."""

    generation_id: str
    vector_points: int
    graph_nodes: int
    graph_edges: int


async def run_smoke() -> SmokeSummary:
    """Run real embedding, Qdrant, and FalkorDB indexing twice and clean up."""

    from configuration import (
        embedding_config,
        falkordb_config,
        indexing_config,
        qdrant_config,
    )
    from records import CharacterTokenCounter, build_indexing_request

    from harborrag_adapters.models.embed import HarborEmbedClient
    from harborrag_adapters.repositories.graph import HarborGraphDBClient
    from harborrag_adapters.repositories.vector import HarborVectorDBClient
    from harborrag_engine.ingestion.indexing import (
        GraphIndexService,
        IndexingService,
        IndexingStatus,
        VectorIndexService,
        deterministic_vector_point_id,
    )

    config = indexing_config()
    request = build_indexing_request(config)
    embed_client = HarborEmbedClient.from_config(embedding_config())
    vector_repository = HarborVectorDBClient.default().create_from_config(qdrant_config())
    graph_repository = HarborGraphDBClient.default().create_from_config(falkordb_config())
    vector_service = VectorIndexService(
        embed_client=embed_client,
        vector_repository=vector_repository,
        token_counter=CharacterTokenCounter(),
    )
    graph_service = GraphIndexService(graph_repository=graph_repository)
    service = IndexingService(
        vector_service=vector_service,
        graph_service=graph_service,
    )
    graph_plan = graph_service.plan(request)
    point_ids = [
        deterministic_vector_point_id(
            tenant_id=request.chunking.manifest.tenant_id,
            collection=config.vector_collection,
            generation_id=request.generation_id,
            chunk_revision_id=str(record.chunk_revision_id),
            embedding_configuration_fingerprint=(
                config.embedding_configuration_fingerprint
            ),
        )
        for record in request.chunking.chunks
    ]

    async with embed_client, vector_repository, graph_repository:
        require_healthy(await vector_repository.health())
        require_healthy(await graph_repository.health())
        try:
            summary = await _exercise(
                service=service,
                request=request,
                vector_repository=vector_repository,
                graph_repository=graph_repository,
                point_ids=point_ids,
                graph_plan=graph_plan,
                success_status=IndexingStatus.SUCCEEDED,
            )
        except BaseException as error:
            try:
                await _cleanup(
                    request=request,
                    vector_repository=vector_repository,
                    graph_repository=graph_repository,
                    graph_plan=graph_plan,
                )
            except Exception as cleanup_error:
                error.add_note(f"cleanup also failed: {safe_error(cleanup_error)}")
            raise
        await _cleanup(
            request=request,
            vector_repository=vector_repository,
            graph_repository=graph_repository,
            graph_plan=graph_plan,
        )
        return summary


async def _exercise(
    *,
    service,
    request,
    vector_repository,
    graph_repository,
    point_ids: list[str],
    graph_plan,
    success_status,
) -> SmokeSummary:
    first = await service.index(request)
    second = await service.index(request)
    for result in (first, second):
        if result.status is not success_status or not result.vector_valid or not result.graph_valid:
            detail = "; ".join(result.validation_errors) or result.status.value
            raise AssertionError(f"combined indexing did not succeed: {detail}")

    points = await vector_repository.get(
        request.config.vector_collection,
        point_ids,
        context=request.context,
    )
    scanned = await vector_repository.scan(
        request.config.vector_collection,
        limit=10,
        cursor=None,
        context=request.context,
    )
    if {point.id for point in points} != set(point_ids):
        raise AssertionError("Qdrant did not return every staged vector point")
    if {point.id for point in scanned.points} != set(point_ids):
        raise AssertionError("Qdrant retry created missing or duplicate point identities")
    if any(
        point.payload.get("index_state") != "staged"
        or point.payload.get("is_active") is not False
        for point in points
    ):
        raise AssertionError("Qdrant points became active during staging")

    nodes = await graph_repository.get_nodes(
        [node.id for node in graph_plan.nodes],
        context=request.context,
    )
    edges = await graph_repository.get_edges(
        [edge.id for edge in graph_plan.edges],
        context=request.context,
    )
    if len(nodes) != len(graph_plan.nodes) or len(edges) != len(graph_plan.edges):
        raise AssertionError("FalkorDB retry created missing or duplicate graph identities")
    if any(
        node.properties.get("index_state") != "staged"
        or node.properties.get("is_active") is not False
        for node in nodes
    ):
        raise AssertionError("FalkorDB nodes became active during staging")
    return SmokeSummary(
        generation_id=request.generation_id,
        vector_points=len(points),
        graph_nodes=len(nodes),
        graph_edges=len(edges),
    )


async def _cleanup(*, request, vector_repository, graph_repository, graph_plan) -> None:
    errors: list[Exception] = []
    operations = (
        graph_repository.delete_edges(
            [edge.id for edge in graph_plan.edges],
            context=request.context,
        ),
        graph_repository.delete_nodes(
            [node.id for node in graph_plan.nodes],
            context=request.context,
        ),
    )
    for operation in operations:
        try:
            await operation
        except Exception as exc:
            errors.append(exc)
    try:
        if await vector_repository.collection_exists(
            request.config.vector_collection,
            context=request.context,
        ):
            await vector_repository.delete_collection(
                request.config.vector_collection,
                context=request.context,
            )
    except Exception as exc:
        errors.append(exc)
    if errors:
        raise ExceptionGroup("indexing smoke cleanup failed", errors)


def main() -> int:
    """Run the standalone smoke check with documented exit-code semantics."""

    load_env()
    requirements = {
        "litellm": 'Install it with: uv pip install -e "packages/harborrag-adapters[llm]"',
        "qdrant_client": (
            'Install it with: uv pip install -e "packages/harborrag-adapters[qdrant]"'
        ),
        "falkordb": (
            'Install it with: uv pip install -e "packages/harborrag-adapters[falkordb]"'
        ),
    }
    available = [
        dependency_available(name, hint) for name, hint in requirements.items()
    ]
    if not all(available):
        return 2
    try:
        summary = asyncio.run(run_smoke())
    except SmokeNotConfigured as exc:
        print(f"Indexing smoke not configured: {safe_error(exc)}")
        return 2
    except SmokeConfigurationError as exc:
        print(f"Indexing smoke configuration failed: {safe_error(exc)}")
        return 1
    except Exception as exc:
        print(f"Indexing smoke failed: {safe_error(exc)}")
        return 1
    print(
        "Indexing smoke passed: "
        f"generation={summary.generation_id!r}, "
        f"vectors={summary.vector_points}, "
        f"nodes={summary.graph_nodes}, edges={summary.graph_edges}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
