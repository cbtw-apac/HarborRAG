"""Remove one connection's documents from every store.

Handles:
- retiring each published document of the connection, which queues the same
  projection cleanup a removed source item gets
- draining that cleanup so the vectors and graph versions are gone
- dropping the connection's source scopes from the graph
- optionally purging its Postgres history (runs, documents, scopes)

Does NOT touch the connection row or its secrets; the caller removes those last.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_core.ports import KnowledgeGraphRepositoryPort
from harborrag_core.storage import StorageOperationContext

from .cleanup import ProjectionCleanupService

logger = logging.getLogger("harborrag.runtime.ingestion.connection_removal")

_RETIREMENT_CONCURRENCY = 8
_CLEANUP_BATCH = 100


class ConnectionRemovalIncompleteError(RuntimeError):
    """Some projections could not be cleaned; the removal must run again."""


@dataclass(frozen=True, slots=True)
class ConnectionRemovalSummary:
    """What one removal pass did."""

    documents_retired: int
    cleanup_completed: int
    source_scopes: int
    purged: bool


class ConnectionRemovalService:
    """Retire, clean and optionally purge everything one connection ingested."""

    def __init__(
        self,
        *,
        control: IngestionControlPlaneDatabase,
        cleanup: ProjectionCleanupService,
        graph: KnowledgeGraphRepositoryPort,
    ) -> None:
        self._control = control
        self._cleanup = cleanup
        self._graph = graph

    async def remove(
        self, *, tenant_id: str, source_key: str, purge: bool = False
    ) -> ConnectionRemovalSummary:
        """Remove the connection's documents; safe to run again after a failure.

        Args:
            tenant_id: Tenant that owns the connection.
            source_key: The connection's key in ingestion records.
            purge: Also delete its runs, documents and scopes from Postgres.

        Returns:
            Counts of what this pass removed.

        Raises:
            ConnectionRemovalIncompleteError: A projection cleanup failed.
        """
        records = self._control.connection_records
        document_ids = await records.active_document_ids(
            tenant_id=tenant_id, connection_id=source_key
        )
        await self._retire(document_ids)
        completed = await self._drain_cleanup(tenant_id, document_ids)
        scopes = await records.source_scope_ids(tenant_id=tenant_id, connection_id=source_key)
        context = StorageOperationContext.system(tenant_id, operation_kind="delete_connection")
        for scope in scopes:
            await self._graph.delete_source_scope(scope, context=context)
        if purge:
            await records.purge(tenant_id=tenant_id, connection_id=source_key)
        logger.info(
            "Connection removal completed documents=%d cleaned=%d scopes=%d purged=%s",
            len(document_ids),
            completed,
            len(scopes),
            purge,
        )
        return ConnectionRemovalSummary(len(document_ids), completed, len(scopes), purge)

    async def _retire(self, document_ids: Sequence[str]) -> None:
        """Retire every document with bounded concurrency."""
        slots = asyncio.Semaphore(_RETIREMENT_CONCURRENCY)

        async def retire(document_id: str) -> None:
            async with slots:
                await self._control.publisher.retire_removed(document_id=document_id)

        await asyncio.gather(*(retire(document_id) for document_id in document_ids))

    async def _drain_cleanup(self, tenant_id: str, document_ids: Sequence[str]) -> int:
        """Run the documents' pending cleanup jobs until none remain."""
        completed = 0
        for start in range(0, len(document_ids), _CLEANUP_BATCH):
            batch = list(document_ids[start : start + _CLEANUP_BATCH])
            while True:
                result = await self._cleanup.run_documents(
                    tenant_id=tenant_id, document_ids=batch, limit=_CLEANUP_BATCH
                )
                completed += result.completed
                if result.failed:
                    raise ConnectionRemovalIncompleteError(
                        f"{result.failed} projection cleanups failed; removal will retry"
                    )
                if result.claimed == 0:
                    break
        return completed
