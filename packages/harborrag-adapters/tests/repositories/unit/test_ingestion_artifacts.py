from __future__ import annotations

import pytest

from harborrag_adapters.repositories.object_store import (
    ARTIFACT_BUCKET,
    ChunkArtifactReader,
    ChunkArtifactWriter,
    ImmutableArtifact,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    IngestionArtifactLayout,
    MemoryObjectStore,
    RawDocumentArtifactRepository,
)
from harborrag_core.chunking import (
    ChunkHierarchy,
    ChunkKind,
    ChunkRecord,
    ChunkSecurity,
    ConnectorType,
    DocumentKind,
    RecordKind,
)
from harborrag_core.contracts import HarborConflictError
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.schemas.storage import StorageOperationContext


def context() -> StorageOperationContext:
    return StorageOperationContext.system(tenant_id="tenant-a")


def chunk(chunk_id: str, ordinal: int, content: str) -> ChunkRecord:
    return ChunkRecord(
        strategy_version="strategy-1",
        logical_chunk_id=f"logical-chunk:{ordinal}",
        chunk_id=chunk_id,
        connector_type=ConnectorType.LOCAL,
        document_kind=DocumentKind.LOCAL_FILE,
        record_kind=RecordKind.EVIDENCE,
        chunk_kind=ChunkKind.TEXT,
        tenant_id="tenant-a",
        connection_id="connection-1",
        source_scope_id="scope-1",
        source_item_id="release-guide.md",
        source_version="source-version-1",
        document_id="document-1",
        document_version_id="version-1",
        ordinal=ordinal,
        content=content,
        embedding_text=f"Release guide\nDeployment\n\n{content}",
        search_text=f"Release guide Deployment {content}",
        content_hash=f"hash-{ordinal}",
        token_count=len(content.split()),
        hierarchy=ChunkHierarchy(
            document_title="Release guide",
            section_path=("Deployment",),
        ),
        security=ChunkSecurity(permission_set_id="permission-set:public"),
    )


@pytest.mark.asyncio
async def test_immutable_write_replay_is_noop_and_content_change_conflicts() -> None:
    store = MemoryObjectStore()
    async with store:
        writer = ImmutableArtifactWriter(store)
        artifact = ImmutableArtifact(
            bucket=ARTIFACT_BUCKET,
            key="canonical/document-1/version-1.json",
            payload=b'{"content":"stable"}',
            media_type="application/json",
            artifact_kind="canonical-document",
        )

        first = await writer.put(artifact, context=context())
        replayed = await writer.put(artifact, context=context())

        assert first == replayed
        with pytest.raises(HarborConflictError, match="different content"):
            await writer.put(
                ImmutableArtifact(
                    bucket=artifact.bucket,
                    key=artifact.key,
                    payload=b'{"content":"changed"}',
                    media_type=artifact.media_type,
                    artifact_kind=artifact.artifact_kind,
                ),
                context=context(),
            )


@pytest.mark.asyncio
async def test_chunk_jsonl_has_exact_index_and_supports_range_loading() -> None:
    store = MemoryObjectStore()
    async with store:
        chunks = (
            chunk("chunk-1", 0, "The activity timeout is 30 seconds."),
            chunk("chunk-2", 1, "Retries use exponential backoff."),
        )
        writer = ChunkArtifactWriter(ImmutableArtifactWriter(store))
        reader = ChunkArtifactReader(ImmutableArtifactReader(store))

        artifacts = await writer.put(
            document_id="document-1",
            document_version_id="version-1",
            chunks=chunks,
            context=context(),
        )

        assert artifacts.chunks.key == IngestionArtifactLayout.chunks("document-1", "version-1")
        assert artifacts.index.key == IngestionArtifactLayout.chunk_index("document-1", "version-1")
        assert artifacts.entries[1].byte_offset > artifacts.entries[0].byte_offset
        reference = artifacts.content_reference("chunk-2")
        assert reference.model_dump() == {
            "bucket": ARTIFACT_BUCKET,
            "object_key": IngestionArtifactLayout.chunks("document-1", "version-1"),
            "byte_offset": artifacts.entries[1].byte_offset,
            "byte_length": artifacts.entries[1].byte_length,
        }
        assert (
            await reader.get_chunk(
                artifacts,
                "chunk-2",
                context=context(),
            )
            == chunks[1]
        )
        assert await reader.get_reference(reference, context=context()) == chunks[1]
        with pytest.raises(KeyError, match="not present"):
            artifacts.content_reference("missing")
        assert (
            await reader.get_all(
                artifacts.chunks,
                context=context(),
            )
            == chunks
        )
        index_bytes = await store.get_bytes(
            artifacts.index.bucket,
            artifacts.index.key,
            byte_range=None,
            context=context(),
        )
        assert index_bytes.count(b"\n") == 2
        assert b'"chunk_id":"chunk-1"' in index_bytes


@pytest.mark.asyncio
async def test_raw_capture_preserves_original_bytes_and_replay_metadata() -> None:
    store = MemoryObjectStore()
    async with store:
        artifacts = RawDocumentArtifactRepository(
            ImmutableArtifactWriter(store),
            ImmutableArtifactReader(store),
        )
        raw = RawDocument(
            id="confluence://OPS/42",
            source="https://wiki.example/pages/42",
            content="<h1>Release</h1>",
            content_type="text/html",
            metadata={"title": "Release", "version": 7},
            raw={
                "id": "42",
                "body": {"storage": {"value": "<h1>Release</h1>"}},
            },
        )

        reference = await artifacts.put(
            connector=ConnectorType.CONFLUENCE,
            document_id="document-1",
            document=raw,
            context=context(),
        )
        replayed = await artifacts.get(reference, context=context())

        assert reference.source_artifact.key.endswith("/source")
        assert "/metadata/" in reference.metadata_artifact.key
        assert reference.metadata_artifact.key.endswith(".json")
        assert replayed.content == b"<h1>Release</h1>"
        assert replayed.metadata == raw.metadata
        assert replayed.raw == raw.raw


@pytest.mark.asyncio
async def test_chunk_artifact_rejects_empty_required_set() -> None:
    store = MemoryObjectStore()
    async with store:
        writer = ChunkArtifactWriter(ImmutableArtifactWriter(store))
        with pytest.raises(ValueError, match="at least one"):
            await writer.put(
                document_id="document-1",
                document_version_id="version-1",
                chunks=(),
                context=context(),
            )


def test_artifact_layout_matches_release_contract() -> None:
    assert (
        IngestionArtifactLayout.raw(
            ConnectorType.CONFLUENCE,
            "document-1",
            "content-hash",
        )
        == "raw/confluence/document-1/content-hash/source"
    )
    assert (
        IngestionArtifactLayout.canonical("document-1", "version-1")
        == "canonical/document-1/version-1.json"
    )
    assert (
        IngestionArtifactLayout.vector_projection("document-1", "version-1")
        == "projections/document-1/version-1/vector.jsonl"
    )
