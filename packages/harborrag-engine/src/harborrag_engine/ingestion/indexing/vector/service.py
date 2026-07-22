from __future__ import annotations

from harborrag_core.contracts.chunking import TokenCounter
from harborrag_core.models.protocols import AsyncHarborEmbedClientProtocol
from harborrag_core.ports.indexing import VectorIndexRepositoryPort
from harborrag_core.schemas.vector import VectorCollectionSpec, VectorPoint

from ..batching import EmbeddingBatchPlanner
from ..diff import IncrementalChunkDiffer
from ..embedding import EmbeddingService
from ..errors import VectorIndexValidationError
from ..preparation import EmbeddingInputPreparer
from ..schemas import ChunkDiffResult, IndexingRequest
from .planner import VectorMutationPlanner
from .schemas import VectorIndexResult
from .validation import VectorValidationService


class VectorIndexService:
    """Write and validate staged vector upserts without activating a generation."""

    def __init__(
        self,
        *,
        embed_client: AsyncHarborEmbedClientProtocol,
        vector_repository: VectorIndexRepositoryPort,
        token_counter: TokenCounter,
        differ: IncrementalChunkDiffer | None = None,
        batch_planner: EmbeddingBatchPlanner | None = None,
        mutation_planner: VectorMutationPlanner | None = None,
        validator: VectorValidationService | None = None,
    ) -> None:
        """Initialize provider-neutral embedding and vector boundaries."""

        self._vector_repository = vector_repository
        self._differ = differ or IncrementalChunkDiffer()
        self._preparer = EmbeddingInputPreparer(token_counter)
        self._batch_planner = batch_planner or EmbeddingBatchPlanner()
        self._embedding = EmbeddingService(embed_client)
        self._mutation_planner = mutation_planner or VectorMutationPlanner()
        self._validator = validator or VectorValidationService()

    async def stage(
        self,
        request: IndexingRequest,
        diff: ChunkDiffResult | None = None,
    ) -> VectorIndexResult:
        """Embed, persist, and validate staged vector mutations."""

        config = request.config
        fingerprint = config.embedding_configuration_fingerprint
        diff = diff or self._differ.compare(
            request.chunking.manifest,
            request.active_manifest,
            target_embedding_configuration_fingerprint=fingerprint,
            active_embedding_configuration_fingerprint=(
                request.active_embedding_configuration_fingerprint
            ),
        )
        prepared = self._preparer.prepare(request.chunking.chunks, config)
        batches = self._batch_planner.plan(diff, prepared, config)
        embeddings = await self._embedding.embed(batches, config)
        plan = self._mutation_planner.plan(
            generation_id=request.generation_id,
            tenant_id=request.chunking.manifest.tenant_id,
            diff=diff,
            embeddings=embeddings,
            config=config,
            active_generation_id=request.active_generation_id,
            active_embedding_configuration_fingerprint=(
                request.active_embedding_configuration_fingerprint
            ),
        )

        persisted: tuple[VectorPoint, ...] = ()
        if plan.points:
            await self._vector_repository.ensure_collection(
                VectorCollectionSpec(
                    name=config.vector_collection,
                    dimension=config.embedding_dimensions,
                    distance=config.vector_distance,
                    metadata_indexes=list(config.vector_metadata_indexes),
                ),
                context=request.context,
            )
            await self._vector_repository.upsert(
                config.vector_collection,
                plan.points,
                context=request.context,
            )
            persisted = tuple(
                await self._vector_repository.get(
                    config.vector_collection,
                    [point.id for point in plan.points],
                    context=request.context,
                )
            )

        validation = self._validator.validate(plan, persisted)
        if not validation.valid:
            raise VectorIndexValidationError("; ".join(validation.errors))
        return VectorIndexResult(
            diff=diff,
            batches=batches,
            plan=plan,
            validation=validation,
        )
