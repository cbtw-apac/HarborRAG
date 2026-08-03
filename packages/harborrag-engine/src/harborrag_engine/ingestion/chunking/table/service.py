from __future__ import annotations

from dataclasses import replace

from harborrag_core.chunking import ChunkRecord, TableProjectionType
from harborrag_core.contracts import TokenCounter

from ..config import ChunkingPlan
from .classifier import TableShapeClassifier
from .factory import TableChunkFactory
from .models import (
    PlannedTableChunk,
    TableChunkingRequest,
    TableChunkingResult,
    TableChunkRole,
    TablePlan,
    TableShape,
)
from .planner import TableChunkPlanner
from .rendering import TableRenderer
from .validator import TableChunkValidator


class CanonicalTableChunker:
    """Classify, plan, build, cap, and validate canonical table chunks."""

    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter
        self._classifier = TableShapeClassifier(token_counter)
        self._planner = TableChunkPlanner(token_counter)
        self._factory = TableChunkFactory(token_counter)
        self._validator = TableChunkValidator(token_counter)

    def chunk(
        self,
        request: TableChunkingRequest,
        plan: ChunkingPlan,
    ) -> TableChunkingResult:
        artifact = request.artifact
        classification = self._classifier.classify(artifact, plan.table_policy)
        table_plan = self._planner.plan(
            artifact,
            classification,
            plan.table_policy,
        )
        table_plan = self._small_table_fallback(request, plan, table_plan)
        records: list[ChunkRecord] = []
        warnings = [*classification.warnings, *table_plan.warnings]
        evidence_count = 0
        dense_tokens = 0
        capped = any("chunk cap reached" in warning for warning in table_plan.warnings)
        if capped:
            warnings.append("table dense evidence safeguard reached; remaining evidence omitted")
        evidence_capped = False
        for planned in table_plan.chunks:
            if planned.role == TableChunkRole.EVIDENCE and evidence_capped:
                continue
            built = self._factory.build(
                request,
                classification,
                planned,
                plan,
                ordinal_start=len(records),
            )
            if planned.role == TableChunkRole.EVIDENCE:
                next_evidence_count = evidence_count + len(built)
                next_dense_tokens = dense_tokens + sum(record.token_count for record in built)
                if (
                    next_evidence_count > plan.table_policy.maximum_evidence_chunks_per_table
                    or next_dense_tokens > plan.table_policy.maximum_dense_table_tokens
                ):
                    capped = True
                    evidence_capped = True
                    warnings.append(
                        "table dense evidence safeguard reached; remaining evidence omitted"
                    )
                    continue
                evidence_count = next_evidence_count
                dense_tokens = next_dense_tokens
            records.extend(built)
        record_tuple = tuple(records)
        quality = self._validator.validate(
            artifact,
            classification,
            record_tuple,
            plan,
            allow_partial_coverage=capped,
        )
        return TableChunkingResult(
            artifact=artifact,
            classification=classification,
            chunks=record_tuple,
            quality=quality,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _small_table_fallback(
        self,
        request: TableChunkingRequest,
        plan: ChunkingPlan,
        table_plan: TablePlan,
    ) -> TablePlan:
        classification = table_plan.classification
        if classification.shape != TableShape.SMALL:
            return table_plan
        renderer = TableRenderer(request.artifact)
        rows = renderer.data_row_indices
        if not rows:
            return table_plan
        evidence = renderer.render(
            classification,
            PlannedTableChunk(
                role=TableChunkRole.EVIDENCE,
                projection_type=TableProjectionType.ROWS,
                row_start=rows[0],
                row_end=rows[-1],
                selected_column_indices=tuple(range(request.artifact.column_count)),
            ),
            plan.table_policy.route_preview_rows,
        )
        approximate_prefix = request.document_title + "".join(
            value for _, value in sorted(request.source_context.items())
        )
        if self._token_counter.count(f"{approximate_prefix}\n\n{evidence}") <= (
            plan.hard_maximum_tokens
        ):
            return table_plan
        long_classification = replace(classification, shape=TableShape.LONG)
        fallback = self._planner.plan(
            request.artifact,
            long_classification,
            plan.table_policy,
        )
        return TablePlan(
            classification=classification,
            chunks=fallback.chunks,
            warnings=(
                *fallback.warnings,
                "small table exceeded hard token budget; used long-table row grouping",
            ),
        )
