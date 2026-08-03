from __future__ import annotations

import pytest

from harborrag_core.chunking import ChunkKind, CitationLocator, ConnectorType, RecordKind
from harborrag_core.ingestion import (
    ContentReference,
    VectorEvidenceRecord,
    VectorPayload,
    VectorProjectionBatch,
    VectorRouteRecord,
)
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import SparseVector, VectorIndexRecord
from harborrag_engine.ingestion import (
    EVIDENCE_INDEX,
    ROUTE_INDEX,
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


def point(
    point_id: str,
    *,
    kind: RecordKind,
) -> VectorRouteRecord | VectorEvidenceRecord:
    record_type = VectorRouteRecord if kind == RecordKind.ROUTE else VectorEvidenceRecord
    return record_type(
        point_id=point_id,
        tenant_id="tenant-1",
        dense_vector=(0.1, 0.2, 0.3),
        sparse_vector=SparseVector(indices=[1], values=[1.0]),
        payload=VectorPayload(
            chunk_id=f"chunk-{point_id}",
            logical_chunk_id=f"logical-chunk-{point_id}",
            document_id="document-1",
            document_version_id="version-1",
            record_kind=kind,
            chunk_kind=ChunkKind.TEXT,
            connector_type=ConnectorType.LOCAL,
            source_scope_id="scope-1",
            content_reference=ContentReference(
                bucket="harborrag-artifacts",
                object_key="chunks/document-1/version-1.jsonl",
                byte_offset=0,
                byte_length=10,
            ),
            preview="preview",
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
        route_records=(point("route", kind=RecordKind.ROUTE),),
        evidence_records=(point("evidence", kind=RecordKind.EVIDENCE),),
    )

    await store.provision(context=context)
    await store.stage(batch, context=context)
    await store.stage(batch, context=context)
    verification = await store.verify(batch, context=context)

    assert {spec.index_name for spec in repository.specs} == {
        ROUTE_INDEX,
        EVIDENCE_INDEX,
    }
    assert all(spec.dense_vector_name == "dense" for spec in repository.specs)
    assert all(spec.sparse_vector_name == "sparse" for spec in repository.specs)
    assert all(spec.sparse_idf is True for spec in repository.specs)
    assert verification.valid is True
    assert repository.upsert_calls == 4
    assert sum(len(points) for points in repository.points.values()) == 2


@pytest.mark.asyncio
async def test_store_verification_rejects_missing_and_mismatched_points() -> None:
    repository = InMemoryVectorRepository()
    store = VectorProjectionStore(
        repository,  # type: ignore[arg-type]
        VectorProjectionPolicy(dimension=3),
    )
    context = StorageOperationContext.system(tenant_id="tenant-1")
    batch = VectorProjectionBatch.assemble(
        route_records=(point("route", kind=RecordKind.ROUTE),),
        evidence_records=(point("evidence", kind=RecordKind.EVIDENCE),),
    )
    await store.provision(context=context)
    await store.stage(batch, context=context)
    stored = repository.points[ROUTE_INDEX]["route"]
    repository.points[ROUTE_INDEX]["route"] = stored.model_copy(
        update={
            "payload": {
                **stored.payload,
                "record_kind": "evidence",
            }
        }
    )
    repository.points[EVIDENCE_INDEX].clear()

    verification = await store.verify(batch, context=context)

    assert verification.valid is False
    assert verification.missing_point_ids == ("evidence",)
    assert verification.mismatched_payload_point_ids == ("route",)
