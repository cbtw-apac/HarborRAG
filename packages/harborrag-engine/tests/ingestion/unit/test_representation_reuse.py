from __future__ import annotations

import pytest

from harborrag_core.chunking import (
    ChunkHierarchy,
    ChunkKind,
    ChunkRecord,
    ChunkSecurity,
    ConnectorType,
    DocumentKind,
    RecordKind,
)
from harborrag_core.ingestion import (
    ChunkRepresentation,
    RepresentationSet,
)
from harborrag_core.schemas.vector import SparseVector
from harborrag_engine.ingestion.representations.reuse import (
    RepresentationReuseService,
)


def _chunk(
    chunk_id: str,
    logical_id: str,
    *,
    role: str,
    content: str,
    version: str,
) -> ChunkRecord:
    record_kind = RecordKind.ROUTE if role == "route" else RecordKind.EVIDENCE
    return ChunkRecord(
        strategy_version="strategy-v1",
        chunk_id=chunk_id,
        logical_chunk_id=logical_id,
        content_hash=f"hash-{content}",
        connector_type=ConnectorType.LOCAL,
        document_kind=DocumentKind.LOCAL_FILE,
        record_kind=record_kind,
        chunk_kind=ChunkKind.TEXT,
        tenant_id="tenant-a",
        connection_id="local-test",
        source_scope_id="docs",
        source_item_id="page-1",
        source_version="source-v1",
        document_id="document-1",
        document_version_id=version,
        ordinal=0 if role == "route" else 1,
        content=content,
        embedding_text=f"Document: Guide\n\n{content}",
        search_text=content,
        token_count=len(content.split()),
        hierarchy=ChunkHierarchy(document_title="Guide"),
        security=ChunkSecurity(permission_set_id="permission-set:test"),
    )


class RecordingEncoder:
    def __init__(self) -> None:
        self.chunk_ids: list[str] = []

    async def encode(self, chunks):
        self.chunk_ids.extend(str(chunk.chunk_id) for chunk in chunks)
        return RepresentationSet(
            document_id=chunks[0].document_id,
            document_version_id=chunks[0].document_version_id,
            dense_profile_id="dense-v1",
            sparse_profile_id="bm25-v1",
            dense_dimension=2,
            records=tuple(
                ChunkRepresentation(
                    chunk_id=str(chunk.chunk_id),
                    dense_vector=[0.5, 0.5],
                    sparse_vector=SparseVector(indices=[1], values=[1.0]),
                )
                for chunk in chunks
            ),
        )


@pytest.mark.asyncio
async def test_metadata_refresh_reencodes_route_but_reuses_evidence() -> None:
    old_route = _chunk(
        "route-old",
        "route-logical",
        role="route",
        content="Guide\nlabels: old",
        version="version-old",
    )
    old_evidence = _chunk(
        "evidence-old",
        "evidence-logical",
        role="content",
        content="Stable evidence.",
        version="version-old",
    )
    new_route = _chunk(
        "route-new",
        "route-logical",
        role="route",
        content="Guide\nlabels: new",
        version="version-new",
    )
    new_evidence = _chunk(
        "evidence-new",
        "evidence-logical",
        role="content",
        content="Stable evidence.",
        version="version-new",
    )
    previous = RepresentationSet(
        document_id="document-1",
        document_version_id="version-old",
        dense_profile_id="dense-v1",
        sparse_profile_id="bm25-v1",
        dense_dimension=2,
        records=(
            ChunkRepresentation(
                chunk_id="route-old",
                dense_vector=[1.0, 0.0],
                sparse_vector=SparseVector(indices=[1], values=[1.0]),
            ),
            ChunkRepresentation(
                chunk_id="evidence-old",
                dense_vector=[0.0, 1.0],
                sparse_vector=SparseVector(indices=[2], values=[2.0]),
            ),
        ),
    )
    encoder = RecordingEncoder()
    service = RepresentationReuseService(encoder)  # type: ignore[arg-type]

    result = await service.encode(
        (new_route, new_evidence),
        previous_chunks=(old_route, old_evidence),
        previous_representations=previous,
    )

    assert encoder.chunk_ids == ["route-new"]
    assert result.records[1].chunk_id == "evidence-new"
    assert result.records[1].dense_vector == [0.0, 1.0]
