"""Independent exact vector verification before generated-view publication."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from harborrag_adapters.repositories.object_store import ImmutableArtifactReader
from harborrag_adapters.repositories.vector.base import HarborVectorRepository
from harborrag_core.indexing import VectorDistance, VectorIndexRecord, VectorIndexSpec
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import DocumentTopologyBuild
from harborrag_core.topology.derived import DERIVED_VECTOR_PRODUCTS, ContextualManifest
from harborrag_engine.topology.vector_values import canonical_dense_vector

from .derived_coverage import validate_derived_coverage


@dataclass(frozen=True)
class DerivedVectorProjection:
    vectors: HarborVectorRepository
    reader: ImmutableArtifactReader

    async def publish(
        self,
        build: DocumentTopologyBuild,
        manifest: ContextualManifest,
        *,
        context: StorageOperationContext,
    ) -> None:
        index_kinds = {
            product.index_name(manifest.embedding_profile): product.record_kind
            for product in DERIVED_VECTOR_PRODUCTS
        }
        if manifest.index_name not in index_kinds:
            raise ValueError("derived manifest cannot target a baseline or unknown index")
        records = await self._read_records(manifest, context)
        if tuple(row.id for row in records) != manifest.point_ids or len(
            set(manifest.point_ids)
        ) != len(records):
            raise ValueError("derived manifest point IDs do not match artifact")
        for row in records:
            chunks = row.payload.get("chunk_ids", [row.payload.get("chunk_id")])
            if (
                str(row.tenant_id) != str(context.tenant_id)
                or row.payload.get("record_kind") != index_kinds[manifest.index_name]
                or row.payload.get("build_id") != build.build_id
                or row.payload.get("document_id") != build.document_id
                or row.payload.get("document_version_id") != build.document_version_id
                or row.payload.get("projection_point_id") != row.id
                or row.payload.get("embedding_profile") != manifest.embedding_profile
                or len(row.vector) != manifest.dimension
                or row.named_vectors
                or row.sparse_vector is not None
                or tuple(row.vector) != canonical_dense_vector(tuple(row.vector))
                or str(UUID(row.id)) != row.id
                or not isinstance(chunks, list)
                or not set(chunks) <= set(build.chunk_ids)
            ):
                raise ValueError("derived point ownership or profile mismatch")
        validate_derived_coverage(records, build, index_kinds[manifest.index_name])
        await self.vectors.ensure_index(
            VectorIndexSpec(
                index_name=manifest.index_name,
                dimension=manifest.dimension,
                distance=VectorDistance.DOT_PRODUCT,
                metadata_indexes=[
                    "tenant_id",
                    "projection_point_id",
                    "build_id",
                    "document_id",
                    "document_version_id",
                    "embedding_profile",
                ],
            ),
            context=context,
        )
        await self.vectors.upsert_records(manifest.index_name, records, context=context)
        actual = await self.vectors.get_records(
            manifest.index_name, manifest.point_ids, context=context
        )
        if not records_match_at_storage_precision(actual, records):
            raise ValueError("derived vector projection verification failed")

    async def _read_records(
        self, manifest: ContextualManifest, context: StorageOperationContext
    ) -> list[VectorIndexRecord]:
        reference = manifest.artifact
        payload = await self.reader.get(reference, context=context)
        if len(payload) != reference.byte_size or sha256(payload).hexdigest() != reference.sha256:
            raise ValueError("derived vector artifact integrity failure")
        content = json.loads(payload)
        # Contextual artifacts are a record list; parent artifacts retain the
        # descriptions alongside their records. Both freeze identical point data.
        rows = content.get("records") if isinstance(content, dict) else content
        if not isinstance(rows, list):
            raise ValueError("derived vector artifact must contain records")
        return [VectorIndexRecord.model_validate(row) for row in rows]


def records_match_at_storage_precision(
    actual: Sequence[VectorIndexRecord],
    expected: Sequence[VectorIndexRecord],
) -> bool:
    """Compare read-back points after applying Qdrant's float32 storage contract.

    Embedded Qdrant exposes the exact float32 value while the remote JSON API may render
    the same stored value as a shorter decimal (for example ``0.6``). Re-quantizing both
    sides distinguishes that transport representation from a real vector mutation while
    leaving every identity and payload field under exact comparison.
    """

    if len(actual) != len(expected):
        return False
    return _storage_record_map(actual) == _storage_record_map(expected)


def _storage_record_map(
    records: Sequence[VectorIndexRecord],
) -> dict[str, VectorIndexRecord]:
    normalized = (_storage_record(record) for record in records)
    return {record.id: record for record in normalized}


def _storage_record(record: VectorIndexRecord) -> VectorIndexRecord:
    return record.model_copy(update={"vector": list(canonical_dense_vector(tuple(record.vector)))})
