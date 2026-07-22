from __future__ import annotations

from hashlib import sha256

from harborrag_core.schemas.ids import TenantId
from harborrag_core.schemas.vector import VectorPoint

from ..config import IndexingConfig, RemovedVectorPolicy
from ..schemas import ChunkDiffResult, ChunkDiffStatus, EmbeddingRun
from .payload import VectorPayloadBuilder
from .schemas import VectorMutation, VectorMutationAction, VectorMutationPlan


def deterministic_vector_point_id(
    *,
    tenant_id: str,
    collection: str,
    generation_id: str,
    chunk_revision_id: str,
    embedding_configuration_fingerprint: str,
) -> str:
    """Return a deterministic generation-scoped vector point identity."""

    values = (
        "harborrag-vector-point-v2",
        tenant_id,
        collection,
        generation_id,
        chunk_revision_id,
        embedding_configuration_fingerprint,
    )
    if not all(value.strip() for value in values):
        raise ValueError("vector point identity values must be non-empty")
    digest = sha256("\x1f".join(values).encode("utf-8")).hexdigest()
    return f"vector:{digest}"


class VectorMutationPlanner:
    """Translate chunk classifications into explicit vector lifecycle actions."""

    def __init__(self, payload_builder: VectorPayloadBuilder | None = None) -> None:
        """Initialize the planner with an optional payload builder."""

        self._payload_builder = payload_builder or VectorPayloadBuilder()

    def plan(
        self,
        *,
        generation_id: str,
        tenant_id: str,
        diff: ChunkDiffResult,
        embeddings: EmbeddingRun,
        config: IndexingConfig,
        active_generation_id: str | None = None,
        active_embedding_configuration_fingerprint: str | None = None,
    ) -> VectorMutationPlan:
        """Translate a chunk diff and embeddings into vector mutations."""

        fingerprint = config.embedding_configuration_fingerprint
        if embeddings.configuration_fingerprint != fingerprint:
            raise ValueError("embedding run fingerprint does not match indexing config")
        by_revision = {
            str(embedded.record.chunk_revision_id): embedded for embedded in embeddings.chunks
        }
        expected = {
            entry.current.chunk_revision_id
            for entry in diff.for_embedding
            if entry.current is not None
        }
        if set(by_revision) != expected:
            raise ValueError("embedding run does not exactly satisfy the chunk diff")

        mutations: list[VectorMutation] = []
        for entry in diff.entries:
            if entry.status is ChunkDiffStatus.UNCHANGED:
                mutations.append(
                    VectorMutation(
                        action=VectorMutationAction.RETAIN,
                        logical_chunk_id=entry.logical_chunk_id,
                        current_chunk_revision_id=self._revision(entry.current),
                        previous_chunk_revision_id=self._revision(entry.previous),
                        point_id=self._active_point_id(
                            tenant_id=tenant_id,
                            config=config,
                            generation_id=active_generation_id,
                            chunk_revision_id=self._revision(entry.previous),
                            fingerprint=active_embedding_configuration_fingerprint,
                        ),
                    )
                )
                continue
            if entry.status is ChunkDiffStatus.REMOVED:
                mutations.append(
                    VectorMutation(
                        action=self._removed_action(config.removed_vector_policy),
                        logical_chunk_id=entry.logical_chunk_id,
                        previous_chunk_revision_id=self._revision(entry.previous),
                        point_id=self._active_point_id(
                            tenant_id=tenant_id,
                            config=config,
                            generation_id=active_generation_id,
                            chunk_revision_id=self._revision(entry.previous),
                            fingerprint=active_embedding_configuration_fingerprint,
                        ),
                    )
                )
                continue
            if entry.previous is not None:
                mutations.append(
                    VectorMutation(
                        action=VectorMutationAction.RETIRE,
                        logical_chunk_id=entry.logical_chunk_id,
                        current_chunk_revision_id=self._revision(entry.current),
                        previous_chunk_revision_id=self._revision(entry.previous),
                        point_id=self._active_point_id(
                            tenant_id=tenant_id,
                            config=config,
                            generation_id=active_generation_id,
                            chunk_revision_id=self._revision(entry.previous),
                            fingerprint=active_embedding_configuration_fingerprint,
                        ),
                    )
                )
            current_revision = self._revision(entry.current)
            if current_revision is None:
                raise ValueError("vector upsert requires a current chunk revision")
            embedded = by_revision[current_revision]
            point_id = deterministic_vector_point_id(
                tenant_id=tenant_id,
                collection=config.vector_collection,
                generation_id=generation_id,
                chunk_revision_id=current_revision,
                embedding_configuration_fingerprint=fingerprint,
            )
            mutations.append(
                VectorMutation(
                    action=VectorMutationAction.UPSERT,
                    logical_chunk_id=entry.logical_chunk_id,
                    current_chunk_revision_id=current_revision,
                    previous_chunk_revision_id=self._revision(entry.previous),
                    point_id=point_id,
                    point=VectorPoint(
                        id=point_id,
                        tenant_id=TenantId(tenant_id),
                        vector=list(embedded.vector),
                        payload=self._payload_builder.build(
                            embedded.record,
                            generation_id=generation_id,
                            content_reference=(
                                entry.current.body_uri if entry.current is not None else None
                            ),
                            config=config,
                        ),
                    ),
                )
            )
        return VectorMutationPlan(
            collection=config.vector_collection,
            generation_id=generation_id,
            embedding_configuration_fingerprint=fingerprint,
            mutations=tuple(mutations),
            dimension=config.embedding_dimensions,
        )

    @staticmethod
    def _revision(reference: object) -> str | None:
        value = getattr(reference, "chunk_revision_id", None)
        return str(value) if value is not None else None

    @staticmethod
    def _removed_action(policy: RemovedVectorPolicy) -> VectorMutationAction:
        return {
            RemovedVectorPolicy.RETIRE: VectorMutationAction.RETIRE,
            RemovedVectorPolicy.DELETE: VectorMutationAction.DELETE,
            RemovedVectorPolicy.TOMBSTONE: VectorMutationAction.TOMBSTONE,
        }[policy]

    @staticmethod
    def _active_point_id(
        *,
        tenant_id: str,
        config: IndexingConfig,
        generation_id: str | None,
        chunk_revision_id: str | None,
        fingerprint: str | None,
    ) -> str | None:
        if generation_id is None or chunk_revision_id is None or fingerprint is None:
            return None
        return deterministic_vector_point_id(
            tenant_id=tenant_id,
            collection=config.vector_collection,
            generation_id=generation_id,
            chunk_revision_id=chunk_revision_id,
            embedding_configuration_fingerprint=fingerprint,
        )
