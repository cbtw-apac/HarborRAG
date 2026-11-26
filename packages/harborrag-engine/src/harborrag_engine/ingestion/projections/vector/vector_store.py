"""Staging and verification of evidence points in the vector index.

``EVIDENCE_PAYLOAD_INDEXES`` is also the filter vocabulary evidence search
accepts. Adding, removing or renaming a key there changes the public filter
keys, so the MCP guide and the changelog change with it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

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

EVIDENCE_PAYLOAD_INDEXES: tuple[str, ...] = (
    "document_id",
    "document_version_id",
    "record_kind",
    "chunk_kind",
    "connector_type",
    "document_kind",
    "source_scope_id",
    "source_item_id",
    "parent_source_item_id",
    "graph_source_node_key",
    "space_id",
    "page_id",
    "project_id",
    "issue_key",
    "attachment_id",
    "relative_path",
    "language",
    "created_at",
    "last_modified",
    "status",
    "status_category",
    "item_type",
    "priority",
    "assignee",
    "reporter",
    "project_key",
    "labels",
    "components",
)
"""Every payload field a caller can usefully narrow evidence on.

A key outside this list has no index, so filtering on it would scan every
point's on-disk payload, and evidence search rejects it. Connector identifiers
are indexed in pairs, such as a project and its issues, so the more precise
filter is never the unindexed one. An item and the item it is attached to are
both indexed, because tracing a parent filter to its attachments looks up both.
``graph_source_node_key`` joins the graph to its evidence. Times, people,
labels and components are the source's own descriptors that results are
narrowed on.
"""
_MAXIMUM_FILTERED_SCAN_PAGES = 10_000
_SCAN_PAGE_SIZE = 256


@dataclass(frozen=True, slots=True)
class SourceFieldIndex:
    """One ``fields.<key>`` payload path a tenant filters evidence on.

    Attributes:
        path: Payload path, such as ``fields.skill_set``.
        numeric: Whether the index answers ranges. Otherwise values match exactly.
    """

    path: str
    numeric: bool = False


@dataclass(frozen=True, slots=True)
class VectorProjectionPolicy:
    """Vector shape and payload indexes of the evidence collection.

    Attributes:
        dimension: Size of each dense vector, at least 1.
        distance: Similarity metric of the dense vectors.
        dense_vector_name: Name of the dense vector, different from the sparse one.
        sparse_vector_name: Name of the sparse vector.
        field_indexes: Per tenant, the ``fields.<key>`` paths its source scopes
            declare as filterable facets. Only these are indexed, because the
            ``fields`` object holds every typed custom field of a source.
    """

    dimension: int
    distance: VectorDistance = VectorDistance.COSINE
    dense_vector_name: str = "dense"
    sparse_vector_name: str = "sparse"
    field_indexes: Mapping[str, tuple[SourceFieldIndex, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject a dimension below 1 and one name for both vectors.

        Raises:
            ValueError: The dimension is below 1 or the vector names are equal.
        """
        if self.dimension < 1:
            raise ValueError("vector projection dimension must be positive")
        if self.dense_vector_name == self.sparse_vector_name:
            raise ValueError("dense and sparse vector names must differ")

    def index(self, name: str, *, tenant_id: str | None = None) -> VectorIndexSpec:
        """Return the collection spec, with a tenant's declared field indexes.

        Args:
            name: Index name, such as ``EVIDENCE_INDEX``.
            tenant_id: Tenant whose declared ``fields.<key>`` paths are indexed
                too, or ``None`` for the shared payload indexes only.

        Returns:
            The spec, with numeric field paths indexed for ranges.
        """
        declared = self.field_indexes.get(tenant_id, ()) if tenant_id is not None else ()
        paths = sorted({item.path for item in declared} - set(EVIDENCE_PAYLOAD_INDEXES))
        return VectorIndexSpec(
            index_name=name,
            dimension=self.dimension,
            distance=self.distance,
            dense_vector_name=self.dense_vector_name,
            sparse_vector_name=self.sparse_vector_name,
            sparse_idf=True,
            metadata_indexes=[*EVIDENCE_PAYLOAD_INDEXES, *paths],
            float_metadata_indexes=sorted(
                {item.path for item in declared if item.numeric} & set(paths)
            ),
        )


class VectorProjectionStore:
    """Writer and verifier of the dense and sparse evidence points.

    Args:
        repository: Vector index the points are written to.
        policy: Vector shape and payload indexes of the evidence collection.
    """

    def __init__(
        self,
        repository: VectorIndexRepositoryPort,
        policy: VectorProjectionPolicy,
    ) -> None:
        self._repository = repository
        self._policy = policy

    async def provision(self, *, context: StorageOperationContext) -> None:
        """Create the evidence collection, or add the payload indexes it lacks.

        Args:
            context: Storage context of the tenant.
        """
        await self._repository.ensure_index(
            self._policy.index(EVIDENCE_INDEX, tenant_id=str(context.tenant_id)),
            context=context,
        )

    async def stage(
        self,
        batch: VectorProjectionBatch,
        *,
        context: StorageOperationContext,
    ) -> None:
        """Write one document version's evidence points.

        Stored points of the same version that the batch no longer has are
        deleted before the batch is upserted.

        Args:
            batch: Evidence points of one document version.
            context: Storage context of the tenant.
        """
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
        """Compare the stored points with a batch.

        Args:
            batch: Evidence points of one document version.
            context: Storage context of the tenant.

        Returns:
            The points that are missing, have a dense vector of the wrong size,
            have an empty sparse vector or hold a different payload.
        """
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
        """Delete the evidence points a projection manifest lists.

        Args:
            manifest: Manifest of the version whose points are removed.
            context: Storage context of the tenant.
        """
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
        """Return every stored point of the expected points' document versions."""
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
                limit=_SCAN_PAGE_SIZE,
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
        """Return whether the stored payload holds every expected value.

        Extra stored keys are allowed.
        """
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
