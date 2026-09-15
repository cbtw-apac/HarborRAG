"""Indexed immutable-artifact evidence reads."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.chunking import ChunkRecord
from harborrag_core.contracts.errors import HarborCapabilityError
from harborrag_core.indexing import VectorSearchResult
from harborrag_core.ingestion import DocumentIdentityBuilder
from harborrag_core.storage import StorageOperationContext

from ..contracts import EvidenceReadItem, EvidenceReadRequest
from .reader_resources import ReaderResources

_CONTENT_BUDGET_BYTES = 60 * 1024


@dataclass(frozen=True, slots=True)
class _LocatedEvidence:
    requested_index: int
    candidate: VectorSearchResult


class ImmutableEvidenceReader:
    """Use Qdrant only to locate ownership, then read canonical chunk artifacts."""

    def __init__(self, resources: ReaderResources) -> None:
        self._vectors = resources.vectors
        self._validator = resources.validator
        self._permissions = resources.permissions
        self._snapshots = resources.snapshots
        self._chunks = resources.chunks

    async def read(
        self, request: EvidenceReadRequest, context: StorageOperationContext
    ) -> tuple[EvidenceReadItem, ...]:
        if self._snapshots is None:
            raise HarborCapabilityError("immutable evidence reader is not configured")
        located = await self._locate(request, context)
        accepted = await self._authorize(tuple(item.candidate for item in located), context)
        accepted_ids = {item.id for item in accepted}
        output = [EvidenceReadItem(item.chunk_id, "unavailable") for item in request.items]
        used_bytes = 0
        for item in located:
            candidate = item.candidate
            selector = request.items[item.requested_index]
            if candidate.id not in accepted_ids or not _matches_expectation(candidate, selector):
                continue
            canonical = await self._canonical_chunk(candidate, context)
            if canonical is None:
                continue
            size = len(canonical.content.encode("utf-8"))
            if used_bytes + size > _CONTENT_BUDGET_BYTES:
                output[item.requested_index] = EvidenceReadItem(
                    selector.chunk_id, "output_limit"
                )
                continue
            used_bytes += size
            output[item.requested_index] = _available_item(canonical)
        await self._final_authority_check(located, output, request, context)
        return tuple(output)

    async def _locate(
        self, request: EvidenceReadRequest, context: StorageOperationContext
    ) -> tuple[_LocatedEvidence, ...]:
        identity = DocumentIdentityBuilder()
        point_ids = tuple(identity.point_id(chunk_id=item.chunk_id) for item in request.items)
        records = await self._vectors.get_records("evidence", point_ids, context=context)
        by_id = {record.id: record for record in records}
        output = []
        for index, selector in enumerate(request.items):
            point_id = point_ids[index]
            record = by_id.get(point_id)
            if record is None or str(record.tenant_id) != str(request.access.tenant_id):
                continue
            if str(record.payload.get("chunk_id", "")) != selector.chunk_id:
                continue
            output.append(
                _LocatedEvidence(
                    index,
                    VectorSearchResult(
                        id=record.id,
                        score=1.0,
                        raw_score=1.0,
                        payload=record.payload,
                    ),
                )
            )
        return tuple(output)

    async def _canonical_chunk(
        self, candidate: VectorSearchResult, context: StorageOperationContext
    ) -> ChunkRecord | None:
        if self._snapshots is None:
            return None
        document_id = str(candidate.payload.get("document_id", ""))
        snapshot = await self._snapshots.active_snapshot(document_id)
        if (
            snapshot is None
            or str(snapshot.document_version_id)
            != str(candidate.payload.get("document_version_id", ""))
            or snapshot.chunk_artifact is None
            or snapshot.chunk_index_artifact is None
        ):
            return None
        artifacts = await self._chunks.get_artifacts(
            snapshot.chunk_artifact, snapshot.chunk_index_artifact, context=context
        )
        chunk_id = str(candidate.payload.get("chunk_id", ""))
        try:
            chunk = await self._chunks.get_chunk(artifacts, chunk_id, context=context)
        except KeyError:
            return None
        if (
            str(chunk.tenant_id) != str(context.tenant_id)
            or str(chunk.document_id) != document_id
            or str(chunk.document_version_id) != str(snapshot.document_version_id)
        ):
            return None
        return chunk

    async def _authorize(
        self,
        candidates: tuple[VectorSearchResult, ...],
        context: StorageOperationContext,
    ) -> tuple[VectorSearchResult, ...]:
        active = await self._validator.validate(candidates)
        return await self._permissions.validate(active.accepted, context)

    async def _final_authority_check(
        self,
        located: tuple[_LocatedEvidence, ...],
        output: list[EvidenceReadItem],
        request: EvidenceReadRequest,
        context: StorageOperationContext,
    ) -> None:
        final = await self._authorize(
            tuple(
                item.candidate
                for item in located
                if output[item.requested_index].availability == "available"
            ),
            context,
        )
        final_ids = {item.id for item in final}
        for item in located:
            if (
                output[item.requested_index].availability == "available"
                and item.candidate.id not in final_ids
            ):
                output[item.requested_index] = EvidenceReadItem(
                    request.items[item.requested_index].chunk_id, "unavailable"
                )


def _available_item(chunk: ChunkRecord) -> EvidenceReadItem:
    return EvidenceReadItem(
        chunk_id=str(chunk.chunk_id),
        availability="available",
        text=chunk.content,
        document_id=str(chunk.document_id),
        document_version_id=str(chunk.document_version_id),
        document_title=chunk.hierarchy.document_title,
        source_scope_id=chunk.source_scope_id,
        connector_type=chunk.connector_type.value,
        chunk_kind=chunk.chunk_kind.value,
        ordinal=chunk.ordinal,
        section_path=chunk.hierarchy.section_path,
        citation_locator=chunk.citation_locator.model_dump(exclude_none=True),
    )


def _matches_expectation(candidate: VectorSearchResult, selector: object) -> bool:
    expected_document_id = getattr(selector, "expected_document_id", None)
    expected_version_id = getattr(selector, "expected_document_version_id", None)
    return (
        expected_document_id is None
        or expected_document_id == str(candidate.payload.get("document_id", ""))
    ) and (
        expected_version_id is None
        or expected_version_id == str(candidate.payload.get("document_version_id", ""))
    )


__all__ = ["ImmutableEvidenceReader"]
