"""Durable artifact payloads used by ingestion activities."""

from __future__ import annotations

from dataclasses import replace

from harborrag_core.domain.document import Document
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_engine.ingestion.chunking.schemas import ChunkingResult
from harborrag_engine.ingestion.indexing.schemas import IndexingRequest, IndexingResult

from .ingestioncodec import (
    dump_parsed_document,
    dump_payload,
    dump_raw_document,
    load_chunking_result,
    load_document,
    load_raw_document,
    load_source_record,
)
from .schemas import (
    ArtifactActivityInput,
    ArtifactActivityResult,
    ArtifactStage,
    ArtifactStatus,
)


class ArtifactPayloadMixin:
    """Persist and load payloads passed between artifact stages."""

    async def preflight(
        self,
        request: ArtifactActivityInput,
    ) -> ArtifactActivityResult:
        source = await self.load_source(request.state.artifact.source_ref)
        revision_id = self._revision_id(request.state.artifact, source)
        active = await self._active_state(request)
        if active and (
            active.payload.get("active_revision_id") == revision_id
            and active.payload.get("active_indexing_fingerprint")
            == self._indexing_config.configuration_fingerprint
            and active.payload.get("active_chunking_configuration_version")
            == self._chunking_configuration_version
        ):
            return ArtifactActivityResult(
                status=ArtifactStatus.UNCHANGED,
                state=replace(request.state, artifact_revision_id=revision_id),
            )
        await self._reserve_generation(request, revision_id)
        return ArtifactActivityResult(
            status=ArtifactStatus.RUNNING,
            state=replace(
                request.state,
                stage=ArtifactStage.FETCH,
                artifact_revision_id=revision_id,
            ),
        )

    async def load_source(self, source_ref: str) -> SourceRecord:
        return load_source_record(await self._objects.get(source_ref))

    async def persist_snapshot(
        self,
        request: ArtifactActivityInput,
        document: RawDocument,
    ) -> str:
        return await self._objects.put(
            request.tenant_id,
            self._artifact_key(
                request.run_id,
                request.state.artifact.artifact_id,
                "snapshot",
            ),
            dump_raw_document(document),
            kind="raw-document",
        )

    async def load_snapshot(self, snapshot_ref: str) -> RawDocument:
        return load_raw_document(await self._objects.get(snapshot_ref))

    async def persist_parsed_document(
        self,
        request: ArtifactActivityInput,
        parsed: ParsedDocument,
        document: Document,
    ) -> str:
        return await self._objects.put(
            request.tenant_id,
            self._artifact_key(
                request.run_id,
                request.state.artifact.artifact_id,
                "parsed",
            ),
            dump_parsed_document(parsed, document),
            kind="parsed-document",
        )

    async def load_parsed_document(self, parsed_document_ref: str) -> Document:
        return load_document(await self._objects.get(parsed_document_ref))

    async def persist_chunking_result(
        self,
        request: ArtifactActivityInput,
        result: ChunkingResult,
    ) -> str:
        return await self._objects.put(
            request.tenant_id,
            self._artifact_key(
                request.run_id,
                request.state.artifact.artifact_id,
                "chunking",
            ),
            dump_payload("chunking-result", result),
            kind="chunking-result",
        )

    async def load_chunking_result(
        self,
        chunking_result_ref: str,
    ) -> ChunkingResult:
        return load_chunking_result(await self._objects.get(chunking_result_ref))

    async def indexing_request(
        self,
        request: ArtifactActivityInput,
        chunking: ChunkingResult,
    ) -> IndexingRequest:
        active = await self._active_state(request)
        active_chunking = None
        active_ref = active.payload.get("active_chunking_ref") if active else None
        if isinstance(active_ref, str):
            active_chunking = await self.load_chunking_result(active_ref)
        return IndexingRequest(
            chunking=chunking,
            generation_id=request.state.generation_id,
            config=self._indexing_config,
            context=self._context(request.tenant_id, request.run_id),
            active_manifest=(active_chunking.manifest if active_chunking else None),
            active_embedding_configuration_fingerprint=(
                self._optional_text(
                    active.payload.get("active_embedding_fingerprint")
                )
                if active
                else None
            ),
            active_generation_id=(
                self._optional_text(active.payload.get("active_generation_id"))
                if active
                else None
            ),
        )

    async def persist_indexing_result(
        self,
        request: ArtifactActivityInput,
        result: IndexingResult,
    ) -> str:
        return await self._objects.put(
            request.tenant_id,
            self._artifact_key(
                request.run_id,
                request.state.artifact.artifact_id,
                "indexing",
            ),
            dump_payload("indexing-result", result),
            kind="indexing-result",
        )
