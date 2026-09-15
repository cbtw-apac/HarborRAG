"""Read canonical graph/chunk artifacts before any summary model calls."""

import asyncio
from dataclasses import dataclass

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_adapters.repositories.object_store import (
    ChunkArtifactReader,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    ProjectionArtifactRepository,
)
from harborrag_core.chunking import ChunkRecord
from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import (
    ArtifactReference,
    DocumentIdentityBuilder,
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
    ProjectionManifest,
)
from harborrag_core.schemas.ids import TenantId
from harborrag_core.storage import StorageOperationContext
from harborrag_core.summaries import SummaryLease, SummarySnapshot
from harborrag_core.topology.extraction import digest
from harborrag_engine.topology.summary_planner import SummaryPlanNode, summary_plan
from harborrag_runtime.config.settings import RuntimeSettings


@dataclass(frozen=True)
class SummaryInputLoader:
    control: IngestionControlPlaneDatabase
    reader: ImmutableArtifactReader
    writer: ImmutableArtifactWriter
    settings: RuntimeSettings

    async def load(
        self, lease: SummaryLease, snapshot: SummarySnapshot
    ) -> tuple[SummaryPlanNode, ...]:
        repository = self.control.summaries
        documents = await repository.source_documents(lease.tenant_id, lease.source_scope_id)
        if {
            row["document_id"]: row["active_document_version_id"] for row in documents
        } != snapshot.document_versions:
            raise HarborConflictError("summary inputs changed before artifact loading")
        context = StorageOperationContext.system(lease.tenant_id)
        artifacts = ProjectionArtifactRepository(self.writer, self.reader)
        chunks: list[ChunkRecord] = []
        nodes: dict[str, GraphNodeRecord] = {}
        edges: dict[str, GraphEdgeRecord] = {}
        for document in documents:
            manifest = ProjectionManifest.model_validate(document["manifest"])
            if manifest.graph_artifact is None or document["chunk_artifact"] is None:
                raise ValueError("summary canonical graph/chunk manifest unavailable")
            batch_chunks, (batch_nodes, batch_edges) = await asyncio.gather(
                ChunkArtifactReader(self.reader).get_all(
                    ArtifactReference.model_validate(document["chunk_artifact"]),
                    context=context,
                    verify_integrity=True,
                ),
                artifacts.get_graph_projection(manifest.graph_artifact, context=context),
            )
            for chunk in batch_chunks:
                if (
                    str(chunk.tenant_id) != lease.tenant_id
                    or str(chunk.document_version_id) != document["active_document_version_id"]
                ):
                    raise ValueError("summary chunk ownership does not match publication")
            chunks.extend(batch_chunks)
            if len(batch_chunks) > self.settings.topology_max_chunks or len(chunks) > 100000:
                raise ValueError("summary source exceeds canonical chunk budget")
            self._merge_nodes(nodes, batch_nodes, lease)
            edges.update((edge.relation_id, edge) for edge in batch_edges)
        if not documents:
            for node in await repository.retained_nodes(lease.tenant_id, lease.source_scope_id):
                if node.node_kind in {
                    KnowledgeNodeKind.DATA_SOURCE,
                    KnowledgeNodeKind.SOURCE_ENTITY,
                }:
                    nodes[node.node_key] = node
            source_key = DocumentIdentityBuilder().data_source_node_key(
                tenant_id=lease.tenant_id, source_scope_id=lease.source_scope_id
            )
            nodes.setdefault(
                source_key,
                GraphNodeRecord(
                    node_key=source_key,
                    node_kind=KnowledgeNodeKind.DATA_SOURCE,
                    entity_type=GraphEntityType.DATA_SOURCE,
                    logical_id=lease.source_scope_id,
                    ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
                    owner_id=TenantId(lease.tenant_id),
                    source_scope_id=lease.source_scope_id,
                    title=lease.source_scope_id,
                ),
            )
        return summary_plan(tuple(nodes.values()), tuple(edges.values()), tuple(chunks))

    @staticmethod
    def _merge_nodes(
        nodes: dict[str, GraphNodeRecord],
        batch_nodes: tuple[GraphNodeRecord, ...],
        lease: SummaryLease,
    ) -> None:
        for node in batch_nodes:
            if str(node.owner_id) != lease.tenant_id:
                raise ValueError("summary graph ownership does not match tenant")
            if node.source_scope_id not in {None, lease.source_scope_id}:
                continue
            # Shared source observations may have different metadata; select a
            # stable representation, independent of input iteration order.
            previous = nodes.get(node.node_key)
            if previous is None or digest(node.model_dump(mode="json")) < digest(
                previous.model_dump(mode="json")
            ):
                nodes[node.node_key] = node
