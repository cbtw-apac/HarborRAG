"""Provider-neutral activation of validated indexing generations."""

from __future__ import annotations

from harborrag_core.ports.indexing import (
    GraphGenerationRepositoryPort,
    VectorGenerationRepositoryPort,
)

from .schemas import GenerationActivationRequest


class IndexGenerationActivationService:
    """Expose one staged generation and retire its previously active revision.

    Each repository operation is idempotent. The graph mutation runs first so a
    retry after a vector failure can safely repeat it without duplicating data.
    """

    def __init__(
        self,
        vector_repository: VectorGenerationRepositoryPort,
        graph_repository: GraphGenerationRepositoryPort,
    ) -> None:
        self._vector_repository = vector_repository
        self._graph_repository = graph_repository

    async def activate(self, request: GenerationActivationRequest) -> None:
        """Activate the generation in both search providers."""

        await self._graph_repository.activate_generation(
            artifact_id=request.plan.artifact_id,
            generation_id=request.plan.generation_id,
            previous_generation_id=request.plan.previous_generation_id,
            context=request.context,
        )
        await self._vector_repository.activate_generation(
            request.plan.vector_collection,
            artifact_id=request.plan.artifact_id,
            generation_id=request.plan.generation_id,
            activate_ids=request.plan.activate_vector_ids,
            retire_ids=request.plan.retire_vector_ids,
            delete_ids=request.plan.delete_vector_ids,
            tombstone_ids=request.plan.tombstone_vector_ids,
            context=request.context,
        )
