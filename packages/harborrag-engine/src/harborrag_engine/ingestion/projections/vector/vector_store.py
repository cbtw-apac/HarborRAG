from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from harborrag_core.indexing import (
    FilterOperator,
    VectorDistance,
    VectorFilter,
    VectorFilterCondition,
    VectorIndexRecord,
    VectorIndexSpec,
)
from harborrag_core.ingestion import (
    ProjectionManifest,
    VectorEvidenceRecord,
    VectorProjectionBatch,
    VectorProjectionVerification,
)
from harborrag_core.ports.indexing import VectorIndexRepositoryPort
from harborrag_core.storage import StorageOperationContext

from .vector import (
    EVIDENCE_INDEX,
)

_PAYLOAD_INDEXES = [
    "document_id",
    "document_version_id",
    "record_kind",
    "chunk_kind",
    "connector_type",
    "source_scope_id",
    "space_id",
    "project_id",
    "issue_key",
    "language",
]
_MAXIMUM_FILTERED_SCAN_PAGES = 10_000


@dataclass(frozen=True, slots=True)
class VectorProjectionPolicy:
    dimension: int
    distance: VectorDistance = VectorDistance.COSINE
    dense_vector_name: str = "dense"
    sparse_vector_name: str = "sparse"

    def __post_init__(self) -> None:
        if self.dimension < 1:
            raise ValueError("vector projection dimension must be positive")
        if self.dense_vector_name == self.sparse_vector_name:
            raise ValueError("dense and sparse vector names must differ")

    def index(self, name: str) -> VectorIndexSpec:
        return VectorIndexSpec(
            index_name=name,
            dimension=self.dimension,
            distance=self.distance,
            dense_vector_name=self.dense_vector_name,
            sparse_vector_name=self.sparse_vector_name,
            sparse_idf=True,
            metadata_indexes=list(_PAYLOAD_INDEXES),
        )


class VectorProjectionStore:
    """Stage and verify the single dense/sparse evidence projection."""

    def __init__(
        self,
        repository: VectorIndexRepositoryPort,
        policy: VectorProjectionPolicy,
    ) -> None:
        self._repository = repository
        self._policy = policy

    async def provision(self, *, context: StorageOperationContext) -> None:
        await self._repository.ensure_index(
            self._policy.index(EVIDENCE_INDEX),
            context=context,
        )

    async def stage(
        self,
        batch: VectorProjectionBatch,
        *,
        context: StorageOperationContext,
    ) -> None:
        evidence_points = self._points(batch.evidence_records)
        stale_ids = await self._stale_version_point_ids(evidence_points, context=context)
        if stale_ids:
            await self._repository.delete_records(EVIDENCE_INDEX, stale_ids, context=context)
        if evidence_points:
            await self._repository.upsert_records(
                EVIDENCE_INDEX,
                evidence_points,
                context=context,
            )

    async def verify(
        self,
        batch: VectorProjectionBatch,
        *,
        context: StorageOperationContext,
    ) -> VectorProjectionVerification:
        evidence_points = self._points(batch.evidence_records)
        evidence = await self._get(EVIDENCE_INDEX, evidence_points, context=context)
        expected = {point.id: point for point in evidence_points}
        actual = {point.id: point for point in evidence}
        missing = tuple(sorted(expected.keys() - actual.keys()))
        invalid_dense = tuple(
            sorted(
                point_id
                for point_id, point in actual.items()
                if len(point.vector) != self._policy.dimension
            )
        )
        invalid_sparse = tuple(
            sorted(
                point_id
                for point_id, point in actual.items()
                if point.sparse_vector is None
                or not point.sparse_vector.indices
                or not point.sparse_vector.values
            )
        )
        mismatched_payload = tuple(
            sorted(
                point_id
                for point_id, point in actual.items()
                if point_id in expected
                and not self._payload_matches(
                    expected[point_id].payload,
                    point.payload,
                )
            )
        )
        valid = not any((missing, invalid_dense, invalid_sparse, mismatched_payload)) and (
            0,
            len(evidence),
        ) == (
            0,
            len(evidence_points),
        )
        return VectorProjectionVerification(
            valid=valid,
            expected_evidence_count=len(evidence_points),
            actual_evidence_count=len(evidence),
            missing_point_ids=missing,
            invalid_dense_point_ids=invalid_dense,
            invalid_sparse_point_ids=invalid_sparse,
            mismatched_payload_point_ids=mismatched_payload,
        )

    async def delete_manifest(
        self,
        manifest: ProjectionManifest,
        *,
        context: StorageOperationContext,
    ) -> None:
        if manifest.evidence_point_ids:
            await self._repository.delete_records(
                EVIDENCE_INDEX,
                manifest.evidence_point_ids,
                context=context,
            )

    async def _get(
        self,
        collection: str,
        expected: Sequence[VectorIndexRecord],
        *,
        context: StorageOperationContext,
    ) -> list[VectorIndexRecord]:
        if not expected:
            return []
        return await self._repository.get_records(
            collection,
            tuple(point.id for point in expected),
            context=context,
        )

    async def _version_records(
        self,
        expected: Sequence[VectorIndexRecord],
        *,
        context: StorageOperationContext,
    ) -> list[VectorIndexRecord]:
        versions = {
            str(point.payload["document_version_id"])
            for point in expected
            if point.payload.get("document_version_id") is not None
        }
        if not versions:
            return await self._get(EVIDENCE_INDEX, expected, context=context)
        records: list[VectorIndexRecord] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        pages = 0
        filters = VectorFilter(
            must=[
                VectorFilterCondition(
                    field="document_version_id",
                    operator=FilterOperator.IN,
                    value=sorted(versions),
                )
            ]
        )
        while True:
            pages += 1
            if pages > _MAXIMUM_FILTERED_SCAN_PAGES:
                raise ValueError("vector repository exceeded the filtered scan page limit")
            page = await self._repository.scan_records(
                EVIDENCE_INDEX,
                limit=256,
                cursor=cursor,
                filters=filters,
                context=context,
            )
            records.extend(page.records)
            next_cursor = page.next_cursor
            if next_cursor is None:
                return records
            if next_cursor in seen_cursors:
                raise ValueError("vector repository returned a non-advancing scan cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    async def _stale_version_point_ids(
        self,
        expected: Sequence[VectorIndexRecord],
        *,
        context: StorageOperationContext,
    ) -> tuple[str, ...]:
        expected_ids = {point.id for point in expected}
        existing = await self._version_records(expected, context=context)
        return tuple(sorted(point.id for point in existing if point.id not in expected_ids))

    @staticmethod
    def _payload_matches(
        expected: dict[str, object],
        actual: dict[str, object],
    ) -> bool:
        return all(actual.get(key) == value for key, value in expected.items())

    @staticmethod
    def _points(
        records: Sequence[VectorEvidenceRecord],
    ) -> tuple[VectorIndexRecord, ...]:
        return tuple(
            VectorIndexRecord(
                id=record.point_id,
                tenant_id=record.tenant_id,
                vector=list(record.dense_vector),
                sparse_vector=record.sparse_vector,
                payload=record.payload.model_dump(mode="json", exclude_none=True),
            )
            for record in records
        )
