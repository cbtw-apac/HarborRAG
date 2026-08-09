from __future__ import annotations

import pytest

from harborrag_core.chunking import ChunkKind, CitationLocator, ConnectorType, RecordKind
from harborrag_core.ingestion import VectorEvidenceRecord, VectorPayload, VectorProjectionBatch
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import SparseVector, VectorIndexRecord, VectorIndexScanPage
from harborrag_engine.ingestion import (
    EVIDENCE_INDEX,
    VectorProjectionPolicy,
    VectorProjectionStore,
)


class InMemoryVectorRepository:
    def __init__(self) -> None:
        self.specs = []
        self.points: dict[str, dict[str, VectorIndexRecord]] = {}
        self.upsert_calls = 0

    async def ensure_index(self, spec, *, context) -> None:
        del context
        self.specs.append(spec)
        self.points.setdefault(spec.index_name, {})

    async def upsert_records(self, collection, points, *, context) -> None:
        del context
        self.upsert_calls += 1
        target = self.points.setdefault(collection, {})
        target.update((item.id, item) for item in points)

    async def get_records(self, collection, ids, *, context):
        del context
        stored = self.points.get(collection, {})
        return [stored[point_id] for point_id in ids if point_id in stored]

    async def delete_records(self, collection, ids, *, context) -> None:
        del context
        stored = self.points.get(collection, {})
        for point_id in ids:
            stored.pop(point_id, None)

    async def scan_records(self, collection, *, limit, cursor, filters=None, context):
        del context
        records = list(self.points.get(collection, {}).values())
        if filters is not None:
            versions = set(filters.must[0].value)
            records = [
                record
                for record in records
                if record.payload.get("document_version_id") in versions
            ]
        start = int(cursor or 0)
        end = min(len(records), start + limit)
        return VectorIndexScanPage(
            records=records[start:end],
            next_cursor=str(end) if end < len(records) else None,
        )


def point(
    point_id: str,
) -> VectorEvidenceRecord:
    return VectorEvidenceRecord(
        point_id=point_id,
        tenant_id="tenant-1",
        dense_vector=(0.1, 0.2, 0.3),
        sparse_vector=SparseVector(indices=[1], values=[1.0]),
        payload=VectorPayload(
            chunk_id=f"chunk-{point_id}",
            logical_chunk_id=f"logical-chunk-{point_id}",
            document_id="document-1",
            document_version_id="version-1",
            record_kind=RecordKind.EVIDENCE,
            chunk_kind=ChunkKind.TEXT,
            connector_type=ConnectorType.LOCAL,
            source_scope_id="scope-1",
            content="The timeout is 30 seconds.",
            citation_locator=CitationLocator(source_element_ids=("element-1",)),
            quality_score=1.0,
            relative_path="guide.md",
        ),
    )


@pytest.mark.asyncio
async def test_store_provisions_stages_and_verifies_named_hybrid_collections() -> None:
    repository = InMemoryVectorRepository()
    store = VectorProjectionStore(
        repository,  # type: ignore[arg-type]
        VectorProjectionPolicy(dimension=3),
    )
    context = StorageOperationContext.system(tenant_id="tenant-1")
    batch = VectorProjectionBatch.assemble(
        evidence_records=(point("evidence"),),
    )

    await store.provision(context=context)
    await store.stage(batch, context=context)
    await store.stage(batch, context=context)
    verification = await store.verify(batch, context=context)

    assert {spec.index_name for spec in repository.specs} == {EVIDENCE_INDEX}
    assert all(spec.dense_vector_name == "dense" for spec in repository.specs)
    assert all(spec.sparse_vector_name == "sparse" for spec in repository.specs)
    assert all(spec.sparse_idf is True for spec in repository.specs)
    assert verification.valid is True
    assert repository.upsert_calls == 2
    assert sum(len(points) for points in repository.points.values()) == 1


@pytest.mark.asyncio
async def test_store_verification_rejects_missing_and_mismatched_points() -> None:
    repository = InMemoryVectorRepository()
    store = VectorProjectionStore(
        repository,  # type: ignore[arg-type]
        VectorProjectionPolicy(dimension=3),
    )
    context = StorageOperationContext.system(tenant_id="tenant-1")
    batch = VectorProjectionBatch.assemble(
        evidence_records=(point("evidence-1"), point("evidence-2")),
    )
    await store.provision(context=context)
    await store.stage(batch, context=context)
    stored = repository.points[EVIDENCE_INDEX]["evidence-1"]
    repository.points[EVIDENCE_INDEX]["evidence-1"] = stored.model_copy(
        update={
            "payload": {
                **stored.payload,
                "record_kind": "route",
            }
        }
    )
    repository.points[EVIDENCE_INDEX].pop("evidence-2")

    verification = await store.verify(batch, context=context)

    assert verification.valid is False
    assert verification.missing_point_ids == ("evidence-2",)
    assert verification.mismatched_payload_point_ids == ("evidence-1",)


@pytest.mark.asyncio
async def test_store_restage_removes_stale_points_for_same_document_version() -> None:
    repository = InMemoryVectorRepository()
    store = VectorProjectionStore(
        repository,  # type: ignore[arg-type]
        VectorProjectionPolicy(dimension=3),
    )
    context = StorageOperationContext.system(tenant_id="tenant-1")
    first = VectorProjectionBatch.assemble(
        evidence_records=(point("evidence-1"), point("evidence-2")),
    )
    replacement = VectorProjectionBatch.assemble(evidence_records=(point("evidence-1"),))

    await store.stage(first, context=context)
    await store.stage(replacement, context=context)
    verification = await store.verify(replacement, context=context)

    assert verification.valid is True
    assert set(repository.points[EVIDENCE_INDEX]) == {"evidence-1"}
