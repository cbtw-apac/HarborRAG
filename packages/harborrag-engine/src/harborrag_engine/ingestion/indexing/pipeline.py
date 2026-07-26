from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from .config import RemovedVectorPolicy
from .diff import IncrementalChunkDiffer
from .graph.indexer import GraphIndexService
from .graph.schemas import GraphIndexResult, GraphMutationPlan
from .schemas import (
    ChunkDiffStatus,
    GenerationActivationPlan,
    IndexingDiagnostics,
    IndexingFailure,
    IndexingRequest,
    IndexingResult,
    IndexingStatus,
)
from .vector.indexer import VectorIndexService
from .vector.schemas import VectorIndexResult, VectorMutationAction

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
        """Checkpoint independent provider outcomes and resume only missing sides."""

        fingerprint = request.config.embedding_configuration_fingerprint
        diff = self._differ.compare(
            request.chunking.manifest,
            request.active_manifest,
            target_embedding_configuration_fingerprint=fingerprint,
            active_embedding_configuration_fingerprint=(
                request.active_embedding_configuration_fingerprint
            ),
        )
        resume = request.resume_result
        vector_already_valid = bool(resume and resume.vector_valid)
        graph_already_valid = bool(resume and resume.graph_valid)
        graph_plan: GraphMutationPlan | None = None
        graph_plan_error: Exception | None = None
        if not graph_already_valid:
            try:
                graph_plan = self._graph.plan(request, diff)
            except (ValueError, RuntimeError) as exc:
                graph_plan_error = exc

        vector_operation = (
            None if vector_already_valid else self._outcome(self._vector.stage(request, diff))
        )
        graph_operation = (
            None
            if graph_already_valid
            else (
                self._outcome(self._graph.stage(request, graph_plan))
                if graph_plan is not None
                else None
            )
        )
        pending = [
            operation for operation in (vector_operation, graph_operation) if operation is not None
        ]
        completed = await asyncio.gather(*pending)
        completed_iter = iter(completed)
        vector_outcome: object = resume if vector_already_valid else next(completed_iter)
        graph_outcome: object = (
            resume if graph_already_valid else graph_plan_error or next(completed_iter)
        )

        vector_result = vector_outcome if isinstance(vector_outcome, VectorIndexResult) else None
        graph_result = graph_outcome if isinstance(graph_outcome, GraphIndexResult) else None
        vector_valid = vector_already_valid or bool(
            vector_result and vector_result.validation.valid
        )
        graph_valid = graph_already_valid or bool(graph_result and graph_result.validation.valid)
        failures = tuple(
            failure
            for failure in (
                self._failure("vector", vector_outcome, vector_result),
                self._failure("graph", graph_outcome, graph_result),
            )
            if failure is not None
        )
        errors = tuple(f"{failure.component}: {failure.error_type}" for failure in failures)
        successes = int(vector_valid) + int(graph_valid)
        status = (
            IndexingStatus.SUCCEEDED
            if successes == 2
            else IndexingStatus.PARTIAL
            if successes == 1
            else IndexingStatus.FAILED
        )
        graph_nodes = (
            resume.diagnostics.graph_nodes
            if graph_already_valid and resume is not None
            else len(graph_plan.nodes)
            if graph_plan is not None
            else 0
        )
        graph_edges = (
            resume.diagnostics.graph_edges
            if graph_already_valid and resume is not None
            else len(graph_plan.edges)
            if graph_plan is not None
            else 0
        )
        previous_diagnostics = resume.diagnostics if resume is not None else None
        diagnostics = IndexingDiagnostics(
            new_chunks=diff.count(ChunkDiffStatus.NEW),
            unchanged_chunks=diff.count(ChunkDiffStatus.UNCHANGED),
            changed_chunks=diff.count(ChunkDiffStatus.CHANGED),
            removed_chunks=diff.count(ChunkDiffStatus.REMOVED),
            reembedded_chunks=diff.count(ChunkDiffStatus.REEMBED_REQUIRED),
            embedded_chunks=(
                sum(len(batch.inputs) for batch in vector_result.batches)
                if vector_result is not None
                else previous_diagnostics.embedded_chunks
                if vector_already_valid and previous_diagnostics is not None
                else 0
            ),
            embedding_batches=(
                len(vector_result.batches)
                if vector_result is not None
                else previous_diagnostics.embedding_batches
                if vector_already_valid and previous_diagnostics is not None
                else 0
            ),
            vector_upserts=(
                previous_diagnostics.vector_upserts
                if vector_already_valid and previous_diagnostics is not None
                else len(vector_result.plan.points)
                if vector_result is not None
                else 0
            ),
            vector_retentions=diff.count(ChunkDiffStatus.UNCHANGED),
            vector_retirements=(
                diff.count(ChunkDiffStatus.CHANGED)
                + diff.count(ChunkDiffStatus.REEMBED_REQUIRED)
                + diff.count(ChunkDiffStatus.REFRESH_REQUIRED)
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
            vector_valid=vector_valid,
            graph_valid=graph_valid,
            validation_errors=errors,
            diagnostics=diagnostics,
            activation=(
                resume.activation
                if vector_already_valid and resume is not None
                else self._activation_plan(request, vector_result)
            ),
            failures=failures,
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
    def _failure(
        label: str,
        outcome: object,
        result: VectorIndexResult | GraphIndexResult | None,
    ) -> IndexingFailure | None:
        if isinstance(outcome, Exception):
            context = getattr(outcome, "context", None)
            retryable = getattr(context, "retryable", None)
            if not isinstance(retryable, bool):
                retryable = getattr(outcome, "retryable", None)
            if not isinstance(retryable, bool):
                retryable = isinstance(outcome, (TimeoutError, ConnectionError, RuntimeError))
            return IndexingFailure(label, type(outcome).__name__, retryable)
        if result is not None and not result.validation.valid:
            return IndexingFailure(label, "validation_failed", False)
        return None
