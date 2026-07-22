from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from .config import RemovedVectorPolicy
from .diff import IncrementalChunkDiffer
from .graph.schemas import GraphIndexResult, GraphMutationPlan
from .graph.service import GraphIndexService
from .schemas import (
    ChunkDiffStatus,
    GenerationActivationPlan,
    IndexingDiagnostics,
    IndexingRequest,
    IndexingResult,
    IndexingStatus,
)
from .vector.schemas import VectorIndexResult, VectorMutationAction
from .vector.service import VectorIndexService

OutcomeT = TypeVar("OutcomeT")


class IndexingService:
    """Execute vector and graph indexing independently for one generation."""

    def __init__(
        self,
        *,
        vector_service: VectorIndexService,
        graph_service: GraphIndexService,
        differ: IncrementalChunkDiffer | None = None,
    ) -> None:
        """Initialize independent vector and graph indexing services."""

        self._vector = vector_service
        self._graph = graph_service
        self._differ = differ or IncrementalChunkDiffer()

    async def index(self, request: IndexingRequest) -> IndexingResult:
        """Return a complete result even when one provider boundary fails."""

        fingerprint = request.config.embedding_configuration_fingerprint
        diff = self._differ.compare(
            request.chunking.manifest,
            request.active_manifest,
            target_embedding_configuration_fingerprint=fingerprint,
            active_embedding_configuration_fingerprint=(
                request.active_embedding_configuration_fingerprint
            ),
        )
        graph_plan: GraphMutationPlan | None = None
        graph_plan_error: Exception | None = None
        try:
            graph_plan = self._graph.plan(request, diff)
        except (ValueError, RuntimeError) as exc:
            graph_plan_error = exc

        vector_task = self._vector.stage(request, diff)
        if graph_plan is None:
            vector_outcome = await self._outcome(vector_task)
            graph_outcome: object = graph_plan_error or RuntimeError(
                "graph mutation planning failed"
            )
        else:
            vector_outcome, graph_outcome = await asyncio.gather(
                self._outcome(vector_task),
                self._outcome(self._graph.stage(request, graph_plan)),
            )

        vector_result = vector_outcome if isinstance(vector_outcome, VectorIndexResult) else None
        graph_result = graph_outcome if isinstance(graph_outcome, GraphIndexResult) else None
        errors = tuple(
            error
            for error in (
                self._error("vector", vector_outcome),
                self._error("graph", graph_outcome),
            )
            if error is not None
        )
        successes = sum(result is not None for result in (vector_result, graph_result))
        status = (
            IndexingStatus.SUCCEEDED
            if successes == 2
            else IndexingStatus.PARTIAL
            if successes == 1
            else IndexingStatus.FAILED
        )
        graph_nodes = len(graph_plan.nodes) if graph_plan is not None else 0
        graph_edges = len(graph_plan.edges) if graph_plan is not None else 0
        diagnostics = IndexingDiagnostics(
            new_chunks=diff.count(ChunkDiffStatus.NEW),
            unchanged_chunks=diff.count(ChunkDiffStatus.UNCHANGED),
            changed_chunks=diff.count(ChunkDiffStatus.CHANGED),
            removed_chunks=diff.count(ChunkDiffStatus.REMOVED),
            reembedded_chunks=diff.count(ChunkDiffStatus.REEMBED_REQUIRED),
            embedded_chunks=(
                sum(len(batch.inputs) for batch in vector_result.batches)
                if vector_result is not None
                else 0
            ),
            embedding_batches=(len(vector_result.batches) if vector_result is not None else 0),
            vector_upserts=len(diff.for_embedding),
            vector_retentions=diff.count(ChunkDiffStatus.UNCHANGED),
            vector_retirements=(
                diff.count(ChunkDiffStatus.CHANGED)
                + diff.count(ChunkDiffStatus.REEMBED_REQUIRED)
                + (
                    diff.count(ChunkDiffStatus.REMOVED)
                    if request.config.removed_vector_policy is RemovedVectorPolicy.RETIRE
                    else 0
                )
            ),
            vector_deletions=(
                diff.count(ChunkDiffStatus.REMOVED)
                if request.config.removed_vector_policy is RemovedVectorPolicy.DELETE
                else 0
            ),
            vector_tombstones=(
                diff.count(ChunkDiffStatus.REMOVED)
                if request.config.removed_vector_policy is RemovedVectorPolicy.TOMBSTONE
                else 0
            ),
            graph_nodes=graph_nodes,
            graph_edges=graph_edges,
        )
        manifest = request.chunking.manifest
        return IndexingResult(
            artifact_id=manifest.artifact_id,
            artifact_revision_id=manifest.artifact_revision_id,
            generation_id=request.generation_id,
            status=status,
            vector_valid=(vector_result.validation.valid if vector_result is not None else False),
            graph_valid=(graph_result.validation.valid if graph_result is not None else False),
            validation_errors=errors,
            diagnostics=diagnostics,
            activation=self._activation_plan(request, vector_result),
        )

    @staticmethod
    def _activation_plan(
        request: IndexingRequest,
        vector_result: VectorIndexResult | None,
    ) -> GenerationActivationPlan:
        """Retain provider mutation identities without persisting raw vectors."""

        mutations = vector_result.plan.mutations if vector_result is not None else ()

        def identities(action: VectorMutationAction) -> tuple[str, ...]:
            return tuple(
                mutation.point_id
                for mutation in mutations
                if mutation.action is action and mutation.point_id is not None
            )

        manifest = request.chunking.manifest
        return GenerationActivationPlan(
            artifact_id=manifest.artifact_id,
            generation_id=request.generation_id,
            previous_generation_id=request.active_generation_id,
            vector_collection=request.config.vector_collection,
            activate_vector_ids=identities(VectorMutationAction.UPSERT),
            retire_vector_ids=identities(VectorMutationAction.RETIRE),
            delete_vector_ids=identities(VectorMutationAction.DELETE),
            tombstone_vector_ids=identities(VectorMutationAction.TOMBSTONE),
        )

    @staticmethod
    async def _outcome(operation: Awaitable[OutcomeT]) -> OutcomeT | Exception:
        try:
            return await operation
        except Exception as exc:
            return exc

    @staticmethod
    def _error(label: str, outcome: object) -> str | None:
        if isinstance(outcome, Exception):
            return f"{label}: {type(outcome).__name__}: {outcome}"
        return None
