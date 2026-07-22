from __future__ import annotations

import os
from uuid import uuid4

import pytest
from harborrag_adapters.repositories.graph import HarborGraphDBClient
from harborrag_engine.ingestion.indexing import GraphIndexService

from ..unit.indexing_helpers import (
    make_index_request,
    make_manifest,
    make_record,
    make_reference,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_deps]


@pytest.mark.asyncio
async def test_graph_indexing_round_trips_through_falkordb() -> None:
    """Stage deterministic inactive graph records through the real adapter."""

    pytest.importorskip("falkordb")
    if os.getenv("HARBORRAG_FALKORDB_INTEGRATION") != "1":
        pytest.skip("set HARBORRAG_FALKORDB_INTEGRATION=1 for the live service test")

    references = (
        make_reference("logical-1", "revision-1", "hash-1", ordinal=0),
        make_reference("logical-2", "revision-2", "hash-2", ordinal=1),
    )
    manifest = make_manifest(references, artifact_revision_id="artifact-revision-1")
    records = tuple(
        make_record(reference, artifact_revision_id="artifact-revision-1")
        for reference in references
    )
    request = make_index_request(proposed=manifest, records=records)
    options: dict[str, object] = {
        "host": os.getenv("FALKORDB_HOST", "127.0.0.1"),
        "port": int(os.getenv("FALKORDB_PORT", "6379")),
        "graph_name": f"harborrag_indexing_integration_{uuid4().hex}",
        "ssl": os.getenv("FALKORDB_SSL", "false").casefold()
        in {"1", "true", "yes", "on"},
    }
    for option, environment_name in (
        ("username", "FALKORDB_USERNAME"),
        ("password", "FALKORDB_PASSWORD"),
    ):
        if value := os.getenv(environment_name):
            options[option] = value
    repository = HarborGraphDBClient.default().create(
        backend="falkordb",
        instance_name="engine-indexing-integration",
        options=options,
    )
    service = GraphIndexService(graph_repository=repository)
    plan = service.plan(request)

    async with repository:
        try:
            first = await service.stage(request, plan)
            second = await service.stage(request, plan)
            nodes = await repository.get_nodes(
                [node.id for node in plan.nodes],
                context=request.context,
            )
            edges = await repository.get_edges(
                [edge.id for edge in plan.edges],
                context=request.context,
            )

            assert first.validation.valid and second.validation.valid
            assert {node.id for node in nodes} == {node.id for node in plan.nodes}
            assert {edge.id for edge in edges} == {edge.id for edge in plan.edges}
            assert all(node.properties["index_state"] == "staged" for node in nodes)
            assert all(node.properties["is_active"] is False for node in nodes)
        finally:
            try:
                await repository.delete_edges(
                    [edge.id for edge in plan.edges],
                    context=request.context,
                )
            finally:
                await repository.delete_nodes(
                    [node.id for node in plan.nodes],
                    context=request.context,
                )
