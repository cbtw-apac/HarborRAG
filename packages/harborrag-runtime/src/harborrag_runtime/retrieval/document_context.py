"""Ordered, version-bound document context reads."""

from __future__ import annotations

from harborrag_core.chunking import ChunkRecord
from harborrag_core.contracts.errors import HarborCapabilityError
from harborrag_core.contracts.reader import (
    DocumentContextChunk,
    DocumentContextRequest,
    DocumentContextResponse,
)
from harborrag_core.ingestion import ChunkSetArtifacts
from harborrag_core.storage import StorageOperationContext

from .reader_resources import ReaderResources

_CONTENT_BUDGET_BYTES = 60 * 1024


class _ContextUnavailableError(Exception):
    pass


class DocumentContextReader:
    def __init__(self, resources: ReaderResources) -> None:
        self._topology = resources.topology
        self._snapshots = resources.snapshots
        self._chunks = resources.chunks

    async def read(
        self,
        request: DocumentContextRequest,
        request_id: str,
        context: StorageOperationContext,
    ) -> DocumentContextResponse:
        if self._snapshots is None:
            raise HarborCapabilityError("document context reader is not configured")
        if not await self._document_allowed(request):
            return DocumentContextResponse(request_id, "unavailable", request.document_id)
        snapshot = await self._snapshots.active_snapshot(request.document_id)
        if (
            snapshot is None
            or snapshot.chunk_artifact is None
            or snapshot.chunk_index_artifact is None
        ):
            return DocumentContextResponse(request_id, "unavailable", request.document_id)
        version_id = str(snapshot.document_version_id)
        if (
            request.expected_document_version_id is not None
            and request.expected_document_version_id != version_id
        ):
            return DocumentContextResponse(
                request_id, "version_changed", request.document_id, version_id
            )
        artifacts = await self._chunks.get_artifacts(
            snapshot.chunk_artifact,
            snapshot.chunk_index_artifact,
            context=context,
        )
        try:
            start = await self._start(request, artifacts, version_id, context)
            chunks, next_offset, output_limited = await self._window(
                request, artifacts, version_id, start, context
            )
        except _ContextUnavailableError:
            return DocumentContextResponse(request_id, "unavailable", request.document_id)
        if not await self._version_allowed(request, version_id):
            return DocumentContextResponse(request_id, "unavailable", request.document_id)
        outline = (
            tuple(dict.fromkeys(item.section_path for item in chunks if item.section_path))
            if request.include_outline
            else ()
        )
        return DocumentContextResponse(
            request_id,
            "output_limit" if output_limited else "ok",
            request.document_id,
            version_id,
            chunks,
            outline,
            next_offset,
        )

    async def _window(
        self,
        request: DocumentContextRequest,
        artifacts: ChunkSetArtifacts,
        version_id: str,
        start: int,
        context: StorageOperationContext,
    ) -> tuple[tuple[DocumentContextChunk, ...], int | None, bool]:
        chunks: list[DocumentContextChunk] = []
        used_bytes = 0
        next_offset: int | None = None
        output_limited = False
        for index, entry in enumerate(artifacts.entries[start:], start=start):
            if len(chunks) >= request.limit:
                next_offset = index
                break
            chunk = await self._chunks.get_chunk(artifacts, entry.chunk_id, context=context)
            self._validate_chunk(chunk, request, version_id, context)
            size = len(chunk.content.encode("utf-8"))
            if chunks and used_bytes + size > _CONTENT_BUDGET_BYTES:
                next_offset = index
                break
            if not chunks and size > _CONTENT_BUDGET_BYTES:
                next_offset = index + 1
                output_limited = True
                break
            used_bytes += size
            chunks.append(
                DocumentContextChunk(
                    chunk_id=str(chunk.chunk_id),
                    ordinal=chunk.ordinal,
                    text=chunk.content,
                    chunk_kind=chunk.chunk_kind.value,
                    section_path=chunk.hierarchy.section_path,
                    citation_locator=chunk.citation_locator.model_dump(exclude_none=True),
                )
            )
        return tuple(chunks), next_offset, output_limited

    async def _start(
        self,
        request: DocumentContextRequest,
        artifacts: ChunkSetArtifacts,
        version_id: str,
        context: StorageOperationContext,
    ) -> int:
        if request.offset:
            return min(request.offset, len(artifacts.entries))
        if request.anchor_chunk_id is not None:
            result = next(
                (
                    index
                    for index, entry in enumerate(artifacts.entries)
                    if entry.chunk_id == request.anchor_chunk_id
                ),
                None,
            )
            if result is None:
                raise _ContextUnavailableError
            return result
        if request.anchor_section_path:
            for index, entry in enumerate(artifacts.entries):
                chunk = await self._chunks.get_chunk(artifacts, entry.chunk_id, context=context)
                self._validate_chunk(chunk, request, version_id, context)
                if chunk.hierarchy.section_path == request.anchor_section_path:
                    return index
            raise _ContextUnavailableError
        return 0

    async def _document_allowed(self, request: DocumentContextRequest) -> bool:
        if self._topology is None:
            return False
        return request.document_id in await self._topology.authorized_document_ids(
            str(request.access.tenant_id), (request.document_id,), access=request.access
        )

    async def _version_allowed(self, request: DocumentContextRequest, version_id: str) -> bool:
        if not await self._document_allowed(request) or self._snapshots is None:
            return False
        current = await self._snapshots.active_snapshot(request.document_id)
        return current is not None and str(current.document_version_id) == version_id

    @staticmethod
    def _validate_chunk(
        chunk: ChunkRecord,
        request: DocumentContextRequest,
        version_id: str,
        context: StorageOperationContext,
    ) -> None:
        if (
            str(chunk.tenant_id) != str(context.tenant_id)
            or str(chunk.document_id) != request.document_id
            or str(chunk.document_version_id) != version_id
        ):
            raise _ContextUnavailableError


__all__ = ["DocumentContextReader"]
