from __future__ import annotations

from ..diff import IncrementalChunkDiffer
from ..schemas import ChunkDiffResult, ChunkDiffStatus, IndexingRequest
from ..vector.planner import deterministic_vector_point_id
from .projection import UniversalGraphProjector
from .projectionstate import deterministic_graph_node_id
from .schemas import GraphMutationPlan


class GraphMutationPlanner:
    """Build a complete staged graph projection from canonical chunk records."""

    def __init__(self, projector: UniversalGraphProjector | None = None) -> None:
        """Initialize the planner with an optional graph projector."""

        self._projector = projector or UniversalGraphProjector()

    def plan(
        self,
        request: IndexingRequest,
        diff: ChunkDiffResult | None = None,
    ) -> GraphMutationPlan:
        """Build a deterministic staged graph mutation plan."""

        config = request.config
        fingerprint = config.embedding_configuration_fingerprint
        vector_ids: dict[str, str] = {}
        retired_node_ids: list[str] = []
        diff = diff or self._diff(request)
        for entry in diff.entries:
            if entry.current is not None and entry.status is not ChunkDiffStatus.UNCHANGED:
                vector_ids[entry.current.chunk_revision_id] = deterministic_vector_point_id(
                    tenant_id=request.chunking.manifest.tenant_id,
                    collection=config.vector_collection,
                    generation_id=request.generation_id,
                    chunk_revision_id=entry.current.chunk_revision_id,
                    embedding_configuration_fingerprint=fingerprint,
                )
            elif (
                entry.status is ChunkDiffStatus.UNCHANGED
                and entry.previous is not None
                and entry.current is not None
                and request.active_generation_id is not None
                and request.active_embedding_configuration_fingerprint is not None
            ):
                vector_ids[entry.current.chunk_revision_id] = deterministic_vector_point_id(
                    tenant_id=request.chunking.manifest.tenant_id,
                    collection=config.vector_collection,
                    generation_id=request.active_generation_id,
                    chunk_revision_id=entry.previous.chunk_revision_id,
                    embedding_configuration_fingerprint=(
                        request.active_embedding_configuration_fingerprint
                    ),
                )
            if (
                entry.previous is not None
                and entry.status
                in {
                    ChunkDiffStatus.CHANGED,
                    ChunkDiffStatus.REFRESH_REQUIRED,
                    ChunkDiffStatus.REEMBED_REQUIRED,
                    ChunkDiffStatus.REMOVED,
                }
                and request.active_generation_id is not None
            ):
                retired_node_ids.append(
                    deterministic_graph_node_id(
                        namespace=config.graph_namespace,
                        tenant_id=request.chunking.manifest.tenant_id,
                        generation_id=request.active_generation_id,
                        artifact_id=request.chunking.manifest.artifact_id,
                        kind="chunk",
                        key=entry.previous.chunk_revision_id,
                    )
                )

        state, chunk_node_ids = self._projector.project(
            request,
            vector_point_ids=vector_ids,
        )
        manifest = request.chunking.manifest
        return GraphMutationPlan(
            namespace=config.graph_namespace,
            generation_id=request.generation_id,
            artifact_id=manifest.artifact_id,
            artifact_revision_id=manifest.artifact_revision_id,
            nodes=tuple(state.nodes.values()),
            edges=tuple(state.edges.values()),
            chunk_node_ids=chunk_node_ids,
            retired_node_ids=tuple(retired_node_ids),
            capsule_maximum_characters=config.capsule_maximum_characters,
        )

    @staticmethod
    def _diff(request: IndexingRequest) -> ChunkDiffResult:
        return IncrementalChunkDiffer().compare(
            request.chunking.manifest,
            request.active_manifest,
            target_embedding_configuration_fingerprint=(
                request.config.embedding_configuration_fingerprint
            ),
            active_embedding_configuration_fingerprint=(
                request.active_embedding_configuration_fingerprint
            ),
        )
