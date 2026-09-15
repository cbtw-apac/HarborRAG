"""Dependencies shared by bounded reader use cases."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_adapters.repositories.object_store import ChunkArtifactReader
from harborrag_adapters.repositories.vector.base import HarborVectorRepository
from harborrag_core.topology.search import TopologySearchPort
from harborrag_engine.retrieval import ActiveVersionCandidateValidator

from .contracts import DocumentSnapshotReader, KnowledgeGraphReader, SourceCatalogReader
from .permissions import RetrievalPermissions


@dataclass(frozen=True, slots=True)
class ReaderResources:
    vectors: HarborVectorRepository
    validator: ActiveVersionCandidateValidator
    permissions: RetrievalPermissions
    topology: TopologySearchPort | None
    snapshots: DocumentSnapshotReader | None
    chunks: ChunkArtifactReader
    sources: SourceCatalogReader | None
    graph: KnowledgeGraphReader | None


__all__ = ["ReaderResources"]
