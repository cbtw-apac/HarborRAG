from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import SparseEncoderProfile
from harborrag_engine.ingestion import (
    BM25SparseEncoder,
    ChunkRepresentationEncoder,
    ChunkVersionRebinder,
    RepresentationEncodingPolicy,
    RepresentationReuseService,
)

from .chunking_helpers import make_document, make_profile, make_request, make_service


class RecordingEmbedClient:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def aembed(self, *, request):
        self.inputs.extend(request.inputs)
        return SimpleNamespace(
            embeddings=tuple(
                SimpleNamespace(index=index, value=(float(index + 1), 0.5, 0.25))
                for index, _ in enumerate(request.inputs)
            )
        )


def _chunks():
    document = make_document(
        [DocumentElement("p1", "paragraph", "Stable reindex evidence for HARBOR-142.")]
    )
    return (
        make_service(
            make_profile(target=80, maximum=100),
            configuration_version="3",
            create_route_chunks=True,
        )
        .chunk(make_request(document))
        .chunks
    )


def _service(client: RecordingEmbedClient, *, dense: str, sparse: str):
    encoder = ChunkRepresentationEncoder(
        client,  # type: ignore[arg-type]
        BM25SparseEncoder(SparseEncoderProfile(profile_id=sparse)),
        RepresentationEncodingPolicy(
            logical_model="embedding-model",
            dense_profile_id=dense,
            dense_dimension=3,
        ),
    )
    return RepresentationReuseService(encoder)


@pytest.mark.asyncio
async def test_graph_only_reindex_rebinds_without_dense_encoding() -> None:
    old_chunks = _chunks()
    initial_client = RecordingEmbedClient()
    previous = await _service(initial_client, dense="dense-v1", sparse="sparse-v1").encode(
        old_chunks
    )
    rebound = ChunkVersionRebinder().rebind(
        old_chunks,
        document_version_id="document-version:graph-v2",
    )
    graph_client = RecordingEmbedClient()

    result = await _service(graph_client, dense="dense-v1", sparse="sparse-v1").reindex(
        rebound,
        previous_chunks=old_chunks,
        previous_representations=previous,
        regenerate_dense=False,
        regenerate_sparse=False,
    )

    assert graph_client.inputs == []
    assert [record.dense_vector for record in result.records] == [
        record.dense_vector for record in previous.records
    ]
    assert {record.chunk_id for record in result.records} == {
        str(chunk.chunk_id) for chunk in rebound
    }


@pytest.mark.asyncio
async def test_dense_only_reindex_reuses_sparse_lane() -> None:
    old_chunks = _chunks()
    previous = await _service(RecordingEmbedClient(), dense="dense-v1", sparse="sparse-v1").encode(
        old_chunks
    )
    rebound = ChunkVersionRebinder().rebind(
        old_chunks,
        document_version_id="document-version:dense-v2",
    )
    client = RecordingEmbedClient()

    result = await _service(client, dense="dense-v2", sparse="sparse-v1").reindex(
        rebound,
        previous_chunks=old_chunks,
        previous_representations=previous,
        regenerate_dense=True,
        regenerate_sparse=False,
    )

    assert len(client.inputs) == len(rebound)
    assert result.dense_profile_id == "dense-v2"
    assert [record.sparse_vector for record in result.records] == [
        record.sparse_vector for record in previous.records
    ]
