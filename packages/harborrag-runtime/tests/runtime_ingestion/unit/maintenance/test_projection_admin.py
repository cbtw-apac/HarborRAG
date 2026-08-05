"""Tenant-safety tests for projection administration."""

from __future__ import annotations

from typing import Any

import pytest

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.projection_admin import ProjectionAdministrationService


class VectorRepository:
    def __init__(self) -> None:
        self.existing = {"evidence"}
        self.deleted: list[tuple[str, str]] = []

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def index_exists(self, name: str, *, context: Any) -> bool:
        assert str(context.tenant_id) == "ACME"
        return name in self.existing

    async def delete_index(self, name: str, *, context: Any) -> None:
        self.deleted.append((str(context.tenant_id), name))
        self.existing.discard(name)


class GraphRepository:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def tenant_projection_counts(self, *, context: Any) -> tuple[int, int]:
        assert str(context.tenant_id) == "ACME"
        return 7, 5

    async def delete_tenant_projection(self, *, context: Any) -> None:
        self.deleted.append(str(context.tenant_id))


@pytest.mark.asyncio
async def test_inventory_and_vector_only_delete_stay_within_tenant() -> None:
    vectors = VectorRepository()
    graph = GraphRepository()
    service = ProjectionAdministrationService(
        RuntimeSettings(qdrant_collection_prefix="harbor_", falkordb_graph="knowledge"),
        vectors=vectors,  # type: ignore[arg-type]
        graph=graph,  # type: ignore[arg-type]
    )

    inventory = await service.inspect("ACME")
    deletion = await service.delete(
        "ACME",
        confirmation="ACME",
        stores=frozenset({"vector"}),
    )
    await service.close()

    assert [item.physical_name for item in inventory.vector_collections] == [
        "harbor_ACME_evidence",
    ]
    assert (inventory.graph_nodes, inventory.graph_relations) == (7, 5)
    assert vectors.deleted == [("ACME", "evidence")]
    assert graph.deleted == []
    assert deletion.reindex_required is True


@pytest.mark.asyncio
async def test_delete_rejects_mismatched_confirmation_before_storage_calls() -> None:
    vectors = VectorRepository()
    graph = GraphRepository()
    service = ProjectionAdministrationService(
        RuntimeSettings(),
        vectors=vectors,  # type: ignore[arg-type]
        graph=graph,  # type: ignore[arg-type]
    )

    with pytest.raises(HarborValidationError, match="exactly match"):
        await service.delete(
            "ACME",
            confirmation="DEFAULT",
            stores=frozenset({"vector", "graph"}),
        )

    assert vectors.deleted == []
    assert graph.deleted == []
