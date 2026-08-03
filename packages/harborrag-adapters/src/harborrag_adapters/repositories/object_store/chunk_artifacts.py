from __future__ import annotations

import json
from collections.abc import Sequence

from harborrag_adapters.repositories.object_store.ingestion_artifacts import (
    ARTIFACT_BUCKET,
    ImmutableArtifact,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    IngestionArtifactLayout,
)
from harborrag_core.chunking import ChunkRecord
from harborrag_core.ingestion import (
    ArtifactReference,
    ChunkIndexEntry,
    ChunkSetArtifacts,
    ContentReference,
)
from harborrag_core.storage import StorageOperationContext


class ChunkArtifactWriter:
    """Persist one canonical JSONL chunk set and its byte-range sidecar."""

    def __init__(self, artifacts: ImmutableArtifactWriter) -> None:
        self._artifacts = artifacts

    async def put(
        self,
        *,
        document_id: str,
        document_version_id: str,
        chunks: Sequence[ChunkRecord],
        context: StorageOperationContext,
    ) -> ChunkSetArtifacts:
        if not chunks:
            raise ValueError("a canonical chunk artifact must contain at least one chunk")
        payload, entries = self._jsonl(chunks)
        chunk_reference = await self._artifacts.put(
            ImmutableArtifact(
                bucket=ARTIFACT_BUCKET,
                key=IngestionArtifactLayout.chunks(document_id, document_version_id),
                payload=payload,
                media_type="application/x-ndjson",
                artifact_kind="canonical-chunks",
            ),
            context=context,
        )
        index_payload = b"".join(
            (
                (
                    f'{{"chunk_id":{_json_string(entry.chunk_id)},'
                    f'"byte_offset":{entry.byte_offset},'
                    f'"byte_length":{entry.byte_length}}}\n'
                ).encode()
            )
            for entry in entries
        )
        index_reference = await self._artifacts.put(
            ImmutableArtifact(
                bucket=ARTIFACT_BUCKET,
                key=IngestionArtifactLayout.chunk_index(document_id, document_version_id),
                payload=index_payload,
                media_type="application/x-ndjson",
                artifact_kind="chunk-index",
            ),
            context=context,
        )
        return ChunkSetArtifacts(
            chunks=chunk_reference,
            index=index_reference,
            entries=entries,
        )

    @staticmethod
    def _jsonl(
        chunks: Sequence[ChunkRecord],
    ) -> tuple[bytes, tuple[ChunkIndexEntry, ...]]:
        payload = bytearray()
        entries: list[ChunkIndexEntry] = []
        for chunk in chunks:
            encoded = chunk.model_dump_json(exclude_none=True).encode("utf-8")
            entries.append(
                ChunkIndexEntry(
                    chunk_id=str(chunk.chunk_id),
                    byte_offset=len(payload),
                    byte_length=len(encoded),
                )
            )
            payload.extend(encoded)
            payload.extend(b"\n")
        return bytes(payload), tuple(entries)


class ChunkArtifactReader:
    """Read canonical chunks by indexed range or complete replay artifact."""

    def __init__(self, artifacts: ImmutableArtifactReader) -> None:
        self._artifacts = artifacts

    async def get_chunk(
        self,
        artifacts: ChunkSetArtifacts,
        chunk_id: str,
        *,
        context: StorageOperationContext,
    ) -> ChunkRecord:
        entry = next(
            (candidate for candidate in artifacts.entries if candidate.chunk_id == chunk_id),
            None,
        )
        if entry is None:
            raise KeyError(f"chunk is not present in the chunk index: {chunk_id}")
        reference = artifacts.chunks.model_copy(
            update={
                "byte_offset": entry.byte_offset,
                "byte_length": entry.byte_length,
            }
        )
        return ChunkRecord.model_validate_json(
            await self._artifacts.get(reference, context=context)
        )

    async def get_reference(
        self,
        reference: ContentReference,
        *,
        context: StorageOperationContext,
    ) -> ChunkRecord:
        return ChunkRecord.model_validate_json(
            await self._artifacts.get_range(
                bucket=reference.bucket,
                object_key=reference.object_key,
                byte_offset=reference.byte_offset,
                byte_length=reference.byte_length,
                context=context,
            )
        )

    async def get_all(
        self,
        reference: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> tuple[ChunkRecord, ...]:
        payload = await self._artifacts.get(reference, context=context)
        return tuple(
            ChunkRecord.model_validate_json(line) for line in payload.splitlines() if line.strip()
        )

    async def get_artifacts(
        self,
        chunks: ArtifactReference,
        index: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> ChunkSetArtifacts:
        payload = await self._artifacts.get(index, context=context)
        entries = tuple(
            ChunkIndexEntry.model_validate_json(line)
            for line in payload.splitlines()
            if line.strip()
        )
        return ChunkSetArtifacts(chunks=chunks, index=index, entries=entries)


def _json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
