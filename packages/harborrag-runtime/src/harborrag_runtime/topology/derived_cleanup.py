"""Validated cleanup of rebuildable vector products owned by retired topology builds."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from harborrag_core.ports.storage import VectorRepositoryPort
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.derived import DERIVED_VECTOR_PRODUCTS, ContextualManifest
from harborrag_core.topology.permissions import DerivedArtifactRecord

_INDEX_PREFIXES = {
    product.artifact_kind: product.index_prefix for product in DERIVED_VECTOR_PRODUCTS
}


@dataclass(frozen=True)
class RetiredDerivedProjectionCleaner:
    """Remove only exact point IDs frozen in a retired build's canonical manifest."""

    vectors: VectorRepositoryPort

    async def delete(
        self,
        build_id: str,
        records: Sequence[DerivedArtifactRecord],
        *,
        context: StorageOperationContext,
    ) -> int:
        removed = 0
        touched_indexes: set[str] = set()
        for record in records:
            manifest = _validated_manifest(build_id, record)
            if manifest is None or not manifest.point_ids:
                continue
            if not await self.vectors.index_exists(manifest.index_name, context=context):
                continue
            existing = await self.vectors.get_records(
                manifest.index_name,
                manifest.point_ids,
                context=context,
            )
            if any(row.payload.get("build_id") != build_id for row in existing):
                raise ValueError("derived cleanup point is owned by another build")
            if not existing:
                continue
            point_ids = tuple(row.id for row in existing)
            await self.vectors.delete_records(manifest.index_name, point_ids, context=context)
            if await self.vectors.get_records(manifest.index_name, point_ids, context=context):
                raise ValueError("derived vector projection cleanup verification failed")
            removed += len(point_ids)
            touched_indexes.add(manifest.index_name)
        for index_name in sorted(touched_indexes):
            page = await self.vectors.scan_records(
                index_name,
                limit=1,
                cursor=None,
                context=context,
            )
            if not page.records:
                await self.vectors.delete_index(index_name, context=context)
        return removed


def _validated_manifest(
    build_id: str,
    record: DerivedArtifactRecord,
) -> ContextualManifest | None:
    prefix = _INDEX_PREFIXES.get(record.lineage.artifact_kind)
    if prefix is None:
        return None
    if record.lineage.build_id != build_id:
        raise ValueError("derived cleanup manifest belongs to another build")
    manifest = ContextualManifest.model_validate(
        {**record.lineage.metadata, "artifact": record.artifact}
    )
    expected_index = f"{prefix}{manifest.embedding_profile[:24]}"
    if manifest.index_name != expected_index:
        raise ValueError("derived cleanup manifest targets an unexpected index")
    if len(set(manifest.point_ids)) != len(manifest.point_ids):
        raise ValueError("derived cleanup manifest contains duplicate point IDs")
    for point_id in manifest.point_ids:
        if str(UUID(point_id)) != point_id:
            raise ValueError("derived cleanup manifest contains a non-canonical point ID")
    return manifest
