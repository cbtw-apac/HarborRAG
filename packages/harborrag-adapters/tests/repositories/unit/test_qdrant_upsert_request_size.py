"""Each Qdrant upsert request stays under Qdrant's default 32 MiB request limit."""

from __future__ import annotations

from typing import Any

import pytest
from qdrant_client import grpc as qdrant_grpc
from qdrant_client import models as qm
from qdrant_client.conversions.conversion import RestToGrpc

from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import SparseVector, VectorIndexRecord, VectorIndexSpec

from .test_vector_qdrant.fakes import FakeQdrantClient, FakeRawQdrant, make_config

pytestmark = pytest.mark.unit

QDRANT_MAX_REQUEST_BYTES = 32 * 1024 * 1024
DIMENSION = 1536
CHUNKS = 3_400
CONTEXT = StorageOperationContext.system(tenant_id="tenant-a")


class _RecordingRawQdrant(FakeRawQdrant):
    """Raw Qdrant client that keeps every upsert request it receives."""

    def __init__(self) -> None:
        super().__init__()
        self.upserts: list[list[qm.PointStruct]] = []

    async def upsert(self, **kwargs: Any) -> None:
        self.upserts.append(list(kwargs["points"]))


def _records() -> list[VectorIndexRecord]:
    """One large document version: about 9.5 MB of text at 700 tokens a chunk."""
    dense = [0.012345678901234567] * DIMENSION
    sparse = SparseVector(indices=list(range(0, 3_000, 20)), values=[0.5] * 150)
    content = "Release guide section for the demo service. " * 64
    return [
        VectorIndexRecord(
            id=f"00000000-0000-0000-0000-{index:012d}",
            tenant_id="tenant-a",
            vector=dense,
            sparse_vector=sparse,
            payload={"content": content, "document_id": "document-1"},
        )
        for index in range(CHUNKS)
    ]


@pytest.mark.asyncio
async def test_upsert_of_a_large_document_version_keeps_each_request_under_the_limit() -> None:
    raw = _RecordingRawQdrant()
    repository = QdrantVectorRepository(make_config(), client=FakeQdrantClient(raw))  # type: ignore[arg-type]
    repository._specs[repository._queries.spec_key("evidence", CONTEXT)] = VectorIndexSpec(
        index_name="evidence",
        dimension=DIMENSION,
        dense_vector_name="dense",
        sparse_vector_name="sparse",
    )

    await repository.upsert_records("evidence", _records(), context=CONTEXT)

    rest_sizes = [len(qm.PointsList(points=points).model_dump_json()) for points in raw.upserts]
    grpc_sizes = [
        qdrant_grpc.UpsertPoints(
            collection_name="evidence",
            points=[RestToGrpc.convert_point_struct(point) for point in points],
        ).ByteSize()
        for points in raw.upserts
    ]
    assert max(rest_sizes) <= QDRANT_MAX_REQUEST_BYTES
    assert max(grpc_sizes) <= QDRANT_MAX_REQUEST_BYTES
    assert sum(len(points) for points in raw.upserts) == CHUNKS
