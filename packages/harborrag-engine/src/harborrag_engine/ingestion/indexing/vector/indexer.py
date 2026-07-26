from __future__ import annotations

import logging

from harborrag_core.contracts.chunking import TokenCounter
from harborrag_core.ports.indexing import VectorIndexRepositoryPort
from harborrag_core.ports.model_clients import AsyncHarborEmbedClientProtocol
from harborrag_core.schemas.vector import VectorCollectionSpec, VectorPoint

from ..batching import EmbeddingBatchPlanner
from ..diff import IncrementalChunkDiffer
from ..embedding import EmbeddingService
from ..errors import VectorIndexValidationError
from ..preparation import EmbeddingInputPreparer
from ..schemas import ChunkDiffResult, EmbeddedChunk, IndexingRequest
from .planner import VectorMutationPlanner
from .schemas import VectorIndexResult
from .validation import VectorValidationService

logger = logging.getLogger(__name__)


class VectorIndexService:
    """Write and validate staged vector upserts without activating a generation."""

    def __init__(  # noqa: PLR0913
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
        reused = await self._reuse_vectors(request, diff)
        plan = self._mutation_planner.plan(
            generation_id=request.generation_id,
            tenant_id=request.chunking.manifest.tenant_id,
            diff=diff,
            embeddings=embeddings,
            reused=reused,
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
        logger.info(
            "Vector staging completed",
            extra={
                "tenant_id": request.chunking.manifest.tenant_id,
                "generation_id": request.generation_id,
                "embedded_chunks": len(embeddings.chunks),
                "refreshed_chunks": len(reused),
                "vector_upserts": len(plan.points),
            },
        )
        return VectorIndexResult(
            diff=diff,
            batches=batches,
            plan=plan,
            validation=validation,
        )

    async def _reuse_vectors(
        self,
        request: IndexingRequest,
        diff: ChunkDiffResult,
    ) -> tuple[EmbeddedChunk, ...]:
        """Copy content-stable vectors into the proposed metadata generation."""

        if not diff.for_refresh:
            return ()
        generation_id = request.active_generation_id
        fingerprint = request.active_embedding_configuration_fingerprint
        if generation_id is None or fingerprint is None:
            raise VectorIndexValidationError(
                "vector metadata refresh requires an active generation"
            )
        point_ids_list: list[str] = []
        for entry in diff.for_refresh:
            if entry.previous is None:
                raise VectorIndexValidationError(
                    "vector metadata refresh requires a previous chunk"
                )
            point_id = self._mutation_planner.active_point_id(
                tenant_id=request.chunking.manifest.tenant_id,
                config=request.config,
                generation_id=generation_id,
                chunk_revision_id=entry.previous.chunk_revision_id,
                fingerprint=fingerprint,
            )
            if point_id is None:
                raise VectorIndexValidationError(
                    "vector metadata refresh could not resolve the active point"
                )
            point_ids_list.append(point_id)
        point_ids = tuple(point_ids_list)
        points = await self._vector_repository.get(
            request.config.vector_collection,
            point_ids,
            context=request.context,
        )
        by_id = {point.id: point for point in points}
        records = {str(record.chunk_revision_id): record for record in request.chunking.chunks}
        reused: list[EmbeddedChunk] = []
        for entry, point_id in zip(diff.for_refresh, point_ids, strict=True):
            if entry.current is None:
                raise VectorIndexValidationError("vector metadata refresh requires a current chunk")
            point = by_id.get(point_id)
            if point is None:
                raise VectorIndexValidationError(
                    f"active vector {point_id!r} is unavailable for metadata refresh"
                )
            record = records.get(entry.current.chunk_revision_id)
            if record is None:
                raise VectorIndexValidationError(
                    f"current chunk {entry.current.chunk_revision_id!r} is unavailable"
                )
            reused.append(EmbeddedChunk(record=record, vector=tuple(point.vector)))
        return tuple(reused)
