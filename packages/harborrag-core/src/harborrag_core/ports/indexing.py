"""Provider-independent persistence ports required by engine indexing."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphNodeRecord,
    GraphProjectionVerification,
    KnowledgeGraphTraversal,
)
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import VectorIndexRecord, VectorIndexSpec


class VectorIndexRepositoryPort(Protocol):
    """Persist and read provider-independent records in logical indexes."""

    async def ensure_index(
        self,
        spec: VectorIndexSpec,
        *,
        context: StorageOperationContext,
    ) -> None: ...

    async def upsert_records(
        self,
        index_name: str,
        records: Sequence[VectorIndexRecord],
        *,
        context: StorageOperationContext,
    ) -> None: ...

    async def get_records(
        self,
        index_name: str,
        ids: Sequence[str],
        *,
        context: StorageOperationContext,
    ) -> list[VectorIndexRecord]: ...

    async def delete_records(
        self,
        index_name: str,
        ids: Sequence[str],
        *,
        context: StorageOperationContext,
    ) -> None: ...


class KnowledgeGraphRepositoryPort(Protocol):
    """Persist, verify, and read version-addressed knowledge projections."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def write_projection(
        self,
        nodes: Sequence[GraphNodeRecord],
        relations: Sequence[GraphEdgeRecord],
        *,
        context: StorageOperationContext,
    ) -> None: ...

    async def verify_projection(
        self,
        nodes: Sequence[GraphNodeRecord],
        relations: Sequence[GraphEdgeRecord],
        *,
        available_chunk_ids: Sequence[str],
        context: StorageOperationContext,
    ) -> GraphProjectionVerification: ...

    async def traverse(
        self,
        start_node_key: str,
        *,
        max_depth: int,
        max_nodes: int,
        direction: str,
        context: StorageOperationContext,
    ) -> KnowledgeGraphTraversal: ...

    async def delete_version(
        self,
        document_version_id: str,
        *,
        context: StorageOperationContext,
    ) -> None: ...

    async def tenant_projection_counts(
        self,
        *,
        context: StorageOperationContext,
    ) -> tuple[int, int]: ...

    async def delete_tenant_projection(
        self,
        *,
        context: StorageOperationContext,
    ) -> None: ...
