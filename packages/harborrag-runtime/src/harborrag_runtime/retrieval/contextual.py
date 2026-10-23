"""Authorized generated-view search that returns only independent raw source passages."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.indexing import (
    FilterOperator,
    VectorFilter,
    VectorFilterCondition,
    VectorSearchQuery,
    VectorSearchResult,
)
from harborrag_core.ingestion import DocumentIdentityBuilder
from harborrag_core.ports.storage import VectorRepositoryPort
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.derived import ContextualIndexProfile
from harborrag_core.topology.permissions import DerivedArtifactRecord
from harborrag_core.topology.search import DerivedSearchPort, TopologyEvidence

_MAX_ARTIFACTS = 1000
_MAX_CANDIDATES = 100
_MAX_PARENT_PASSAGES = 4


@dataclass(frozen=True)
class _ProjectionHit:
    hit: VectorSearchResult
    artifact: DerivedArtifactRecord
    chunk_ids: tuple[str, ...]
    navigation: tuple[dict[str, object], ...] = ()


class ContextualEvidenceSearch:
    """Registry-approved manifests select physical indexes and exact point scopes."""

    def __init__(
        self,
        repository: DerivedSearchPort,
        vectors: VectorRepositoryPort,
        profile: ContextualIndexProfile,
    ) -> None:
        self._repository = repository
        self._vectors = vectors
        self._profile = profile

    async def search(
        self, query: VectorSearchQuery, *, context: StorageOperationContext
    ) -> tuple[tuple[VectorSearchResult, TopologyEvidence], ...]:
        if len(query.vector) != self._profile.dimension or query.filters is not None:
            # Raw-only metadata predicates are not guaranteed to exist on generated
            # points. Preserve the explicit filtered-flat fallback until adapted.
            return ()
        records = await self._repository.active_derivations(
            str(context.tenant_id), access=context.access, limit=_MAX_ARTIFACTS
        )
        hits: list[_ProjectionHit] = []
        for kind, index_name in (
            ("contextual_chunk", self._profile.index_name),
            ("parent_description", self._profile.parent_index_name),
        ):
            manifests = _manifest_points(records, kind, index_name, self._profile)
            if manifests:
                hits.extend(await self._search_index(query, manifests, index_name, context))
        return await self._raw_passages(hits, min(query.top_k, _MAX_CANDIDATES), context)

    async def _search_index(
        self,
        query: VectorSearchQuery,
        manifests: dict[str, DerivedArtifactRecord],
        index_name: str,
        context: StorageOperationContext,
    ) -> list[_ProjectionHit]:
        scoped = query.model_copy(
            update={
                "index_name": index_name,
                "top_k": min(query.top_k, _MAX_CANDIDATES),
                "filters": VectorFilter(
                    must=[
                        VectorFilterCondition(
                            field="projection_point_id",
                            operator=FilterOperator.IN,
                            value=list(manifests),
                        ),
                        VectorFilterCondition(
                            field="embedding_profile",
                            value=next(iter(manifests.values())).lineage.metadata[
                                "embedding_profile"
                            ],
                        ),
                        VectorFilterCondition(
                            field="build_id",
                            operator=FilterOperator.IN,
                            value=sorted({row.lineage.build_id for row in manifests.values()}),
                        ),
                    ]
                ),
            }
        )
        results = await self._vectors.search(scoped, context=context)
        return [
            parsed for hit in results if (parsed := _projection_hit(hit, manifests)) is not None
        ]

    async def _raw_passages(
        self,
        hits: list[_ProjectionHit],
        limit: int,
        context: StorageOperationContext,
    ) -> tuple[tuple[VectorSearchResult, TopologyEvidence], ...]:
        identity = DocumentIdentityBuilder()
        chunks = tuple(dict.fromkeys(chunk for item in hits for chunk in item.chunk_ids))[
            :_MAX_CANDIDATES
        ]
        records = (
            await self._vectors.get_records(
                "evidence",
                tuple(identity.point_id(chunk_id=chunk) for chunk in chunks),
                context=context,
            )
            if chunks
            else []
        )
        by_chunk = {
            str(row.payload.get("chunk_id")): row
            for row in records
            if str(row.tenant_id) == str(context.tenant_id)
        }
        output: dict[str, tuple[VectorSearchResult, TopologyEvidence]] = {}
        for item in hits:
            for chunk in item.chunk_ids:
                row = by_chunk.get(chunk)
                if row is None or not _raw_owner_matches(row.payload, item):
                    continue
                if row.id != identity.point_id(chunk_id=chunk):
                    continue
                support = TopologyEvidence(
                    chunk,
                    str(row.payload["document_id"]),
                    str(row.payload["document_version_id"]),
                    (item.artifact.lineage.build_id,),
                    derived_artifact_ids=(item.artifact.lineage.artifact_id,),
                    navigation_summaries=item.navigation,
                )
                output.setdefault(
                    chunk,
                    (
                        VectorSearchResult(
                            id=row.id,
                            score=item.hit.score,
                            raw_score=item.hit.raw_score,
                            payload=row.payload,
                        ),
                        support,
                    ),
                )
                if len(output) >= limit:
                    return tuple(output.values())
        return tuple(output.values())


def _manifest_points(
    records: tuple[DerivedArtifactRecord, ...],
    kind: str,
    index_name: str,
    profile: ContextualIndexProfile,
) -> dict[str, DerivedArtifactRecord]:
    output: dict[str, DerivedArtifactRecord] = {}
    fingerprint = (
        profile.parent_fingerprint if kind == "parent_description" else profile.fingerprint
    )
    for record in records:
        metadata = record.lineage.metadata
        if (
            record.lineage.artifact_kind != kind
            or metadata.get("index_name") != index_name
            or metadata.get("embedding_profile") != fingerprint
            or metadata.get("dimension") != profile.dimension
        ):
            continue
        point_ids = metadata.get("point_ids")
        if not isinstance(point_ids, (list, tuple)) or not all(
            isinstance(item, str) for item in point_ids
        ):
            continue
        for point_id in point_ids:
            if isinstance(point_id, str):
                output[point_id] = record
    return output


def _projection_hit(
    hit: VectorSearchResult,
    manifests: dict[str, DerivedArtifactRecord],
) -> _ProjectionHit | None:
    record = manifests.get(hit.id)
    if record is None:
        return None
    payload = hit.payload
    owner = record.lineage.input_document_versions
    if (
        not isinstance(payload.get("document_id"), str)
        or not isinstance(payload.get("document_version_id"), str)
        or payload.get("projection_point_id") != hit.id
        or payload.get("build_id") != record.lineage.build_id
        or payload.get("embedding_profile") != record.lineage.metadata.get("embedding_profile")
        or owner.get(str(payload.get("document_id"))) != payload.get("document_version_id")
    ):
        return None
    if record.lineage.artifact_kind == "contextual_chunk":
        chunk = payload.get("chunk_id")
        if payload.get("record_kind") != "contextual" or not isinstance(chunk, str):
            return None
        return _ProjectionHit(hit, record, (chunk,))
    chunks, citations = payload.get("chunk_ids"), payload.get("cited_chunk_ids")
    description = payload.get("description")
    if (
        payload.get("record_kind") != "parent_description"
        or not isinstance(payload.get("parent_key"), str)
        or payload.get("level") not in {"section", "document", "folder"}
        or not isinstance(chunks, (list, tuple))
        or not isinstance(citations, (list, tuple))
        or not isinstance(description, str)
        or not description.strip()
        or not all(isinstance(chunk, str) for chunk in (*chunks, *citations))
    ):
        return None
    if not set(citations) <= set(chunks):
        return None
    representatives = tuple(dict.fromkeys((*citations, *chunks)))[:_MAX_PARENT_PASSAGES]
    return _ProjectionHit(
        hit,
        record,
        representatives,
        (
            {
                "parent_key": payload.get("parent_key"),
                "description": description,
                "level": payload.get("level"),
                "derived_artifact_id": record.lineage.artifact_id,
                "coverage": "representative_source_passages",
                "input_chunk_count": len(chunks),
            },
        ),
    )


def _raw_owner_matches(payload: dict[str, object], item: _ProjectionHit) -> bool:
    document, version = payload.get("document_id"), payload.get("document_version_id")
    if item.artifact.lineage.artifact_kind == "contextual_chunk" and (
        document != item.hit.payload.get("document_id")
        or version != item.hit.payload.get("document_version_id")
    ):
        return False
    return (
        payload.get("record_kind") == "evidence"
        and isinstance(payload.get("content"), str)
        and isinstance(document, str)
        and isinstance(version, str)
        and item.artifact.lineage.input_document_versions.get(document) == version
    )
