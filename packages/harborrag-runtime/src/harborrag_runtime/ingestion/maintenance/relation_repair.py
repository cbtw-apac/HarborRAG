"""Repair graph relations after source publication or reindexing."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_adapters.repositories.errors import HarborStorageNotFoundError
from harborrag_adapters.repositories.object_store import (
    CanonicalDocumentArtifactRepository,
    ChunkArtifactReader,
)
from harborrag_core.domain.document import Document
from harborrag_core.ingestion import (
    ChangeFingerprintBuilder,
    KnowledgeNodeKind,
    ProcessingProfile,
)
from harborrag_core.ports import KnowledgeGraphRepositoryPort
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.ingestion import (
    GraphDocumentTarget,
    GraphProjectionBuilder,
    GraphProjectionInput,
)

from ..document.models import DocumentReleaseRequest

logger = logging.getLogger("harborrag.runtime.ingestion.relation_repair")


class PlannedRelease(Protocol):
    """Structural input required by relation repair after source dispatch."""

    @property
    def document_id(self) -> str: ...

    @property
    def request(self) -> DocumentReleaseRequest: ...


@dataclass(frozen=True, slots=True)
class RelationRepairResult:
    repaired_documents: int
    resolved_relations: int
    unresolved_relations: int


@dataclass(frozen=True, slots=True)
class _RepairTarget:
    document_id: str
    processing_fingerprint: str
    graph_projection_version: str
    source_scope_id: str | None


class GraphRelationRepairService:
    """Resolve cross-document edges after a bounded source batch publishes."""

    def __init__(
        self,
        *,
        control: IngestionControlPlaneDatabase,
        canonical_artifacts: CanonicalDocumentArtifactRepository,
        chunk_reader: ChunkArtifactReader,
        graph_store: KnowledgeGraphRepositoryPort,
        max_concurrency: int = 8,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("relation repair concurrency must be positive")
        self._control = control
        self._canonical = canonical_artifacts
        self._chunks = chunk_reader
        self._graph = graph_store
        self._builder = GraphProjectionBuilder()
        self._fingerprints = ChangeFingerprintBuilder()
        self._max_concurrency = max_concurrency

    async def repair(
        self,
        planned: Sequence[PlannedRelease],
        *,
        tenant_id: str,
    ) -> RelationRepairResult:
        context = StorageOperationContext.system(tenant_id)
        results = await self._repair_targets(
            tuple(
                _RepairTarget(
                    document_id=item.document_id,
                    processing_fingerprint=self._fingerprints.processing_fingerprint(
                        profile=item.request.processing
                    ),
                    graph_projection_version=item.request.processing.graph_projection_version,
                    source_scope_id=item.request.source_identity.source_scope_id,
                )
                for item in planned
            ),
            context=context,
        )
        return self._result(results)

    async def repair_reindexed(
        self,
        *,
        tenant_id: str,
        processing: ProcessingProfile,
        anchor_document_id: str | None,
        limit: int = 100_000,
    ) -> RelationRepairResult:
        """Restore active source edges after retired target nodes are cleaned."""

        fingerprint = self._fingerprints.processing_fingerprint(profile=processing)
        document_ids = await self._control.document_versions.active_relation_document_ids(
            processing_fingerprint=fingerprint,
            anchor_document_id=anchor_document_id,
            limit=limit,
        )
        context = StorageOperationContext.system(tenant_id)
        results = await self._repair_targets(
            tuple(
                _RepairTarget(
                    document_id=document_id,
                    processing_fingerprint=fingerprint,
                    graph_projection_version=processing.graph_projection_version,
                    source_scope_id=None,
                )
                for document_id in document_ids
            ),
            context=context,
        )
        return self._result(results)

    async def _repair_targets(
        self,
        targets: Sequence[_RepairTarget],
        *,
        context: StorageOperationContext,
    ) -> tuple[tuple[int, int, int], ...]:
        slots = asyncio.Semaphore(self._max_concurrency)

        async def repair(target: _RepairTarget) -> tuple[int, int, int]:
            async with slots:
                return await self._repair_one(
                    target.document_id,
                    expected_processing_fingerprint=target.processing_fingerprint,
                    graph_projection_version=target.graph_projection_version,
                    source_scope_id=target.source_scope_id,
                    context=context,
                )

        return tuple(await asyncio.gather(*(repair(target) for target in targets)))

    @staticmethod
    def _result(results: Sequence[tuple[int, int, int]]) -> RelationRepairResult:
        return RelationRepairResult(
            repaired_documents=sum(result[0] for result in results),
            resolved_relations=sum(result[1] for result in results),
            unresolved_relations=sum(result[2] for result in results),
        )

    async def _repair_one(
        self,
        document_id: str,
        *,
        expected_processing_fingerprint: str,
        graph_projection_version: str,
        source_scope_id: str | None,
        context: StorageOperationContext,
    ) -> tuple[int, int, int]:
        snapshot = await self._control.document_versions.active_snapshot(document_id)
        if snapshot is None:
            return (0, 0, 0)
        if snapshot.fingerprints.processing_fingerprint != expected_processing_fingerprint:
            return (0, 0, 0)
        if snapshot.canonical_artifact is None or snapshot.chunk_artifact is None:
            raise RuntimeError("active document is missing canonical graph inputs")
        try:
            document, chunks = await asyncio.gather(
                self._canonical.get(
                    snapshot.canonical_artifact,
                    context=context,
                ),
                self._chunks.get_all(
                    snapshot.chunk_artifact,
                    context=context,
                ),
            )
        except HarborStorageNotFoundError:
            # Relation repair is a degradable follow-up. Old deployments can
            # retain an active Postgres version after its disposable artifact
            # generation was cleaned. Do not fail or repeatedly retry the
            # entire source ingestion for that stale repair target.
            logger.warning(
                "Skipping relation repair because active artifacts are unavailable "
                "document_id=%s document_version_id=%s",
                document_id,
                snapshot.document_version_id,
            )
            return (0, 0, 1)
        resolved_scope_id = source_scope_id or self._source_scope_id(document)
        source_ids = tuple(dict.fromkeys(relation.target_id for relation in document.relations))
        targets = await self._control.document_versions.resolve_active_sources(
            source_scope_id=resolved_scope_id,
            source_item_ids=source_ids,
        )
        graph = self._builder.build(
            GraphProjectionInput(
                document=document,
                chunks=chunks,
                resolved_targets={
                    source_id: GraphDocumentTarget(
                        source_item_id=target.source_item_id,
                        document_id=target.document_id,
                        document_version_id=target.document_version_id,
                        source_scope_id=target.source_scope_id,
                        title=target.title,
                    )
                    for source_id, target in targets.items()
                },
                graph_projection_version=graph_projection_version,
            )
        )
        document_node_keys = {
            node.node_key
            for node in graph.nodes
            if node.node_kind == KnowledgeNodeKind.SOURCE_ENTITY
        }
        relations = tuple(
            relation
            for relation in graph.relations
            if relation.source_explicit
            and relation.source_node_key in document_node_keys
            and relation.target_node_key in document_node_keys
        )
        if not relations:
            return (0, 0, len(graph.unresolved_relations))
        endpoint_keys = {
            node_key
            for relation in relations
            for node_key in (relation.source_node_key, relation.target_node_key)
        }
        nodes = tuple(node for node in graph.nodes if node.node_key in endpoint_keys)
        await self._graph.write_projection(
            nodes,
            relations,
            context=context,
        )
        verification = await self._graph.verify_projection(
            nodes,
            relations,
            context=context,
        )
        if not verification.valid:
            raise ValueError("repaired graph projection failed verification")
        return (
            1,
            len(relations),
            len(graph.unresolved_relations),
        )

    @staticmethod
    def _source_scope_id(document: Document) -> str:
        value = document.provenance.extra.get("source_scope_id")
        source_scope_id = str(value).strip() if value is not None else ""
        if not source_scope_id:
            raise ValueError("canonical document is missing its source scope")
        return source_scope_id
