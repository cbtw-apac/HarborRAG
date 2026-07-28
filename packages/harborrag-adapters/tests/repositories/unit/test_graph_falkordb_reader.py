from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.repositories.graph.falkordb.config import FalkorDBGraphConfig
from harborrag_adapters.repositories.graph.falkordb.repository import (
    FalkorDBGraphRepository,
)
from harborrag_core.schemas.storage import StorageOperationContext


class FakeQueryResult:
    header = [(1, "edge")]
    result_set = [
        [
            {
                "id": "e1",
                "tenant_id": "tenant-a",
                "source_id": "n1",
                "target_id": "n2",
                "type": "KNOWS",
                "confidence": None,
                "provenance": {},
                "valid_from": "2024-01-01T00:00:00+00:00",
                "valid_to": None,
            }
        ]
    ]


class FakeFalkorDBClient:
    def __init__(self) -> None:
        self.read_calls: list[tuple[str, dict[str, Any]]] = []

    async def read(self, statement: str, parameters: dict[str, Any]) -> FakeQueryResult:
        self.read_calls.append((statement, dict(parameters)))
        return FakeQueryResult()


@pytest.mark.asyncio
async def test_get_edges_maps_provider_rows_into_graph_edges() -> None:
    client = FakeFalkorDBClient()
    repository = FalkorDBGraphRepository(
        FalkorDBGraphConfig(),
        client=client,  # type: ignore[arg-type]
    )
    context = StorageOperationContext(tenant_id="tenant-a")

    edges = await repository.get_edges(["e1"], context=context)

    assert [str(edge.id) for edge in edges] == ["e1"]
    assert edges[0].relationship_type == "KNOWS"
    assert client.read_calls[0][1] == {"tenant_id": "tenant-a", "ids": ["e1"]}
