from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import SparseEncoderProfile
from harborrag_core.models.embed import EmbeddingPurpose
from harborrag_engine.ingestion import (
    BM25SparseEncoder,
    ChunkRepresentationEncoder,
    RepresentationEncodingPolicy,
)

from .chunking_helpers import make_document, make_profile, make_request, make_service


class DeterministicEmbedClient:
    def __init__(self, *, incomplete: bool = False) -> None:
        self.requests = []
        self.incomplete = incomplete

    async def aembed(self, *, request):
        self.requests.append(request)
        inputs = request.inputs[:-1] if self.incomplete else request.inputs
        return SimpleNamespace(
            embeddings=tuple(
                SimpleNamespace(
                    index=index,
                    value=(float(len(text)), 1.0, 0.5),
                )
                for index, text in enumerate(inputs)
            )
        )


def chunks():
    document = make_document(
        [
            DocumentElement(
                "p1",
                "paragraph",
                "Worker timeout for HARBOR-42.",
            )
        ]
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


def encoder(client: DeterministicEmbedClient) -> ChunkRepresentationEncoder:
    return ChunkRepresentationEncoder(
        client,  # type: ignore[arg-type]
        BM25SparseEncoder(SparseEncoderProfile(profile_id="bm25-v1")),
        RepresentationEncodingPolicy(
            logical_model="embedding-model",
            dense_profile_id="dense-v1",
            dense_dimension=3,
            batch_size=1,
        ),
    )


@pytest.mark.asyncio
async def test_encoder_generates_matching_dense_and_sparse_records_in_batches() -> None:
    client = DeterministicEmbedClient()
    canonical_chunks = chunks()

    representations = await encoder(client).encode(canonical_chunks)

    assert len(representations.records) == len(canonical_chunks)
    assert len(client.requests) == len(canonical_chunks)
    assert all(request.purpose == EmbeddingPurpose.DOCUMENT for request in client.requests)
    assert all(request.sensitive is True for request in client.requests)
    assert {record.chunk_id for record in representations.records} == {
        str(chunk.chunk_id) for chunk in canonical_chunks
    }
    assert all(record.sparse_vector.indices for record in representations.records)
    assert "request_id" not in representations.model_dump(mode="json")


@pytest.mark.asyncio
async def test_encoder_rejects_an_incomplete_model_response() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        await encoder(DeterministicEmbedClient(incomplete=True)).encode(chunks())
