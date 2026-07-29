from __future__ import annotations

from collections import Counter

from harborrag_core.chunking import ChunkRecord, TableChunkLocator
from harborrag_core.contracts import TokenCounter
from harborrag_core.domain import TableArtifact

from ..config import ChunkingPlan
from .errors import InvalidTableLocatorError, TableChunkingError
from .models import TableChunkRole, TableClassification, TableQualityMetrics


class TableChunkValidator:
    """Validate exact locators, token limits, provenance, and source coverage."""

    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    def validate(
        self,
        artifact: TableArtifact,
        classification: TableClassification,
        records: tuple[ChunkRecord, ...],
        plan: ChunkingPlan,
        *,
        allow_partial_coverage: bool = False,
    ) -> dict[str, TableQualityMetrics]:
        if not records:
            raise TableChunkingError(f"table {artifact.table_id!r} produced no chunks")
        chunk_ids = [str(record.chunk_id) for record in records]
        if len(set(chunk_ids)) != len(chunk_ids):
            raise TableChunkingError(
                f"table {artifact.table_id!r} produced duplicate table chunk identities"
            )
        reports: dict[str, TableQualityMetrics] = {}
        for record in records:
            reports[str(record.chunk_id)] = self._validate_record(
                artifact,
                classification,
                record,
                plan,
            )
        self._validate_route_and_schema(classification, records)
        if not allow_partial_coverage:
            self._validate_coverage(artifact, classification, records, plan)
        return reports

    def _validate_record(
        self,
        artifact: TableArtifact,
        classification: TableClassification,
        record: ChunkRecord,
        plan: ChunkingPlan,
    ) -> TableQualityMetrics:
        locator = self._validate_locator(artifact, record)
        self._validate_content(record, plan)
        self._validate_provenance(artifact, record, locator)
        role = str(record.metadata.get("table_chunk_role") or "")
        header_complete = role != TableChunkRole.EVIDENCE.value or all(
            artifact.column_names[index] in record.content
            for index in locator.selected_column_indices
        )
        warnings = () if header_complete else ("selected headers are not repeated",)
        return TableQualityMetrics(
            boundary_score=1.0,
            self_containment_score=(
                1.0
                if set(classification.key_column_indices).intersection(
                    locator.selected_column_indices
                )
                or role != TableChunkRole.EVIDENCE.value
                else 0.75
            ),
            header_completeness_score=1.0 if header_complete else 0.0,
            provenance_score=1.0,
            noise_score=0.0,
            warnings=warnings,
        )

    @staticmethod
    def _validate_locator(
        artifact: TableArtifact,
        record: ChunkRecord,
    ) -> TableChunkLocator:
        locator = record.table_locator
        if locator is None:
            raise InvalidTableLocatorError("canonical table chunk is missing table_locator")
        if locator.table_id != artifact.table_id:
            raise InvalidTableLocatorError("table locator references a different table")
        if locator.table_version_id != artifact.table_version_id:
            raise InvalidTableLocatorError("table locator references a different table version")
        if not 0 <= locator.row_start <= locator.row_end < artifact.row_count:
            raise InvalidTableLocatorError("table locator row range is outside the artifact")
        if any(index >= artifact.column_count for index in locator.selected_column_indices):
            raise InvalidTableLocatorError("table locator selected column is outside the artifact")
        if not set(locator.key_column_indices).issubset(locator.selected_column_indices):
            raise InvalidTableLocatorError("repeated key columns are missing from selected columns")
        return locator

    def _validate_content(self, record: ChunkRecord, plan: ChunkingPlan) -> None:
        if not record.content.strip():
            raise TableChunkingError("canonical table chunk content is empty")
        actual_tokens = self._token_counter.count(record.embedding_text)
        if actual_tokens != record.token_count:
            raise TableChunkingError("canonical table chunk token count is inconsistent")
        if actual_tokens > plan.hard_maximum_tokens:
            raise TableChunkingError("canonical table chunk exceeds hard token limit")

    @staticmethod
    def _validate_provenance(
        artifact: TableArtifact,
        record: ChunkRecord,
        locator: TableChunkLocator,
    ) -> None:
        if record.hierarchy.section_path != artifact.section_path:
            raise TableChunkingError("canonical table chunk lost section provenance")
        if locator.tab_path != artifact.tab_path:
            raise TableChunkingError("canonical table chunk lost tab provenance")

    @staticmethod
    def _validate_route_and_schema(
        classification: TableClassification,
        records: tuple[ChunkRecord, ...],
    ) -> None:
        roles = {str(record.metadata.get("table_chunk_role") or "") for record in records}
        if TableChunkRole.ROUTE.value not in roles:
            raise TableChunkingError("table route chunk is missing")
        if classification.shape.value in {"large", "wide", "matrix", "time_series"}:
            if TableChunkRole.SCHEMA.value not in roles:
                raise TableChunkingError("required table schema chunk is missing")

    @staticmethod
    def _validate_coverage(
        artifact: TableArtifact,
        classification: TableClassification,
        records: tuple[ChunkRecord, ...],
        plan: ChunkingPlan,
    ) -> None:
        evidence = [
            record
            for record in records
            if record.metadata.get("table_chunk_role") == TableChunkRole.EVIDENCE.value
        ]
        if classification.shape.value == "large" and not evidence:
            return
        expected_rows = {
            row for row in range(artifact.row_count) if row not in artifact.header_row_indices
        }
        covered_rows = {
            row
            for record in evidence
            if record.table_locator is not None
            for row in range(
                record.table_locator.row_start,
                record.table_locator.row_end + 1,
            )
        }
        if not expected_rows.issubset(covered_rows):
            raise TableChunkingError("table evidence chunks do not cover every source row")
        if classification.shape.value in {"small", "long", "time_series"}:
            row_counts = Counter(
                row
                for record in evidence
                if record.table_locator is not None
                and record.table_locator.fragment_index in {None, 0}
                for row in range(
                    record.table_locator.row_start,
                    record.table_locator.row_end + 1,
                )
            )
            maximum = 2 if plan.table_policy.boundary_row_overlap else 1
            if any(count > maximum for count in row_counts.values()):
                raise TableChunkingError("table evidence contains unexpected row duplication")
