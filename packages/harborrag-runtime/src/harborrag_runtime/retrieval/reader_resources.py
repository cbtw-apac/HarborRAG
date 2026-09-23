"""Dependencies shared by bounded reader use cases."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.ports.artifacts import ChunkArtifactReaderPort
from harborrag_core.ports.storage import VectorRepositoryPort
from harborrag_core.ports.summary_projection import SummaryReaderPort
from harborrag_core.topology.search import TopologySearchPort
from harborrag_engine.retrieval import ActiveVersionCandidateValidator

from .contracts import DocumentSnapshotReader, KnowledgeGraphReader, SourceCatalogReader
from .permissions import RetrievalPermissions


@dataclass(frozen=True, slots=True)
class ReaderResources:
    vectors: VectorRepositoryPort
    validator: ActiveVersionCandidateValidator
    permissions: RetrievalPermissions
    topology: TopologySearchPort | None
    snapshots: DocumentSnapshotReader | None
    chunks: ChunkArtifactReaderPort
    sources: SourceCatalogReader | None
    graph: KnowledgeGraphReader | None
    summaries: SummaryReaderPort | None = None


__all__ = ["ReaderResources"]
