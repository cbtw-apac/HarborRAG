from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence

from harborrag_adapters.repositories.object_store.ingestion_artifacts import (
    ARTIFACT_BUCKET,
    ImmutableArtifact,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    IngestionArtifactLayout,
)
from harborrag_core.domain.document import Document
from harborrag_core.ingestion import (
    ArtifactReference,
    GraphEdgeRecord,
    GraphNodeRecord,
    RepresentationSet,
    VectorEvidenceRecord,
    VectorRouteRecord,
    canonical_document_bytes,
    load_canonical_document,
    reject_runtime_fields,
)
from harborrag_core.storage import StorageOperationContext


class CanonicalDocumentArtifactRepository:
    """Persist and reload immutable canonical document recovery boundaries."""

    def __init__(
        self,
        writer: ImmutableArtifactWriter,
        reader: ImmutableArtifactReader,
    ) -> None:
        self._writer = writer
        self._reader = reader

    async def put(
        self,
        *,
        document_id: str,
        document_version_id: str,
        document: Document,
        context: StorageOperationContext,
    ) -> ArtifactReference:
        return await self._writer.put(
            ImmutableArtifact(
                bucket=ARTIFACT_BUCKET,
                key=IngestionArtifactLayout.canonical(
                    document_id,
                    document_version_id,
                ),
                payload=canonical_document_bytes(document),
                media_type="application/json",
                artifact_kind="canonical-document",
            ),
            context=context,
        )

    async def get(
        self,
        reference: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> Document:
        return load_canonical_document(await self._reader.get(reference, context=context))


class ProjectionArtifactRepository:
    """Persist replayable vector and graph batches before external writes."""

    def __init__(
        self,
        writer: ImmutableArtifactWriter,
        reader: ImmutableArtifactReader,
    ) -> None:
        self._writer = writer
        self._reader = reader

    async def put_relations(
        self,
        *,
        document_id: str,
        document_version_id: str,
        relations: Sequence[GraphEdgeRecord],
        context: StorageOperationContext,
    ) -> ArtifactReference:
        payload = self._jsonl(
            relation.model_dump(mode="json")
            for relation in sorted(relations, key=lambda item: item.relation_id)
        )
        return await self._put(
            key=IngestionArtifactLayout.relations(
                document_id,
                document_version_id,
            ),
            payload=payload,
            kind="canonical-relations",
            context=context,
        )

    async def put_vector_projection(
        self,
        *,
        document_id: str,
        document_version_id: str,
        points: Sequence[VectorRouteRecord | VectorEvidenceRecord],
        context: StorageOperationContext,
    ) -> ArtifactReference:
        payload = self._jsonl(
            point.model_dump(mode="json")
            for point in sorted(points, key=lambda item: item.point_id)
        )
        return await self._put(
            key=IngestionArtifactLayout.vector_projection(
                document_id,
                document_version_id,
            ),
            payload=payload,
            kind="vector-projection",
            context=context,
        )

    async def put_graph_projection(
        self,
        *,
        document_id: str,
        document_version_id: str,
        nodes: Sequence[GraphNodeRecord],
        relations: Sequence[GraphEdgeRecord],
        context: StorageOperationContext,
    ) -> ArtifactReference:
        values = (
            *(
                {"record_type": "node", "value": node.model_dump(mode="json")}
                for node in sorted(nodes, key=lambda item: item.node_key)
            ),
            *(
                {
                    "record_type": "edge",
                    "value": relation.model_dump(mode="json"),
                }
                for relation in sorted(
                    relations,
                    key=lambda item: item.relation_id,
                )
            ),
        )
        return await self._put(
            key=IngestionArtifactLayout.graph_projection(
                document_id,
                document_version_id,
            ),
            payload=self._jsonl(values),
            kind="graph-projection",
            context=context,
        )

    async def put_representation(
        self,
        *,
        document_id: str,
        document_version_id: str,
        encoder_profile: str,
        payload: bytes,
        context: StorageOperationContext,
    ) -> ArtifactReference:
        return await self._writer.put(
            ImmutableArtifact(
                bucket=ARTIFACT_BUCKET,
                key=IngestionArtifactLayout.representation(
                    document_id,
                    document_version_id,
                    encoder_profile,
                ),
                payload=payload,
                media_type="application/octet-stream",
                artifact_kind="chunk-representations",
            ),
            context=context,
        )

    async def put_representation_set(
        self,
        representations: RepresentationSet,
        *,
        encoder_profile: str,
        context: StorageOperationContext,
    ) -> ArtifactReference:
        return await self.put_representation(
            document_id=str(representations.document_id),
            document_version_id=str(representations.document_version_id),
            encoder_profile=encoder_profile,
            payload=representations.model_dump_json().encode("utf-8"),
            context=context,
        )

    async def get_representation_set(
        self,
        reference: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> RepresentationSet:
        return RepresentationSet.model_validate_json(
            await self._reader.get(reference, context=context)
        )

    async def get_vector_projection(
        self,
        reference: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> tuple[VectorRouteRecord | VectorEvidenceRecord, ...]:
        records: list[VectorRouteRecord | VectorEvidenceRecord] = []
        for value in self._load_jsonl(await self._reader.get(reference, context=context)):
            payload = value.get("payload")
            record_kind = payload.get("record_kind") if isinstance(payload, Mapping) else None
            record_type = VectorRouteRecord if record_kind == "route" else VectorEvidenceRecord
            records.append(record_type.model_validate(value))
        return tuple(records)

    async def get_graph_projection(
        self,
        reference: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> tuple[tuple[GraphNodeRecord, ...], tuple[GraphEdgeRecord, ...]]:
        nodes: list[GraphNodeRecord] = []
        relations: list[GraphEdgeRecord] = []
        for record in self._load_jsonl(await self._reader.get(reference, context=context)):
            if record.get("record_type") == "node":
                nodes.append(GraphNodeRecord.model_validate(record["value"]))
            elif record.get("record_type") == "edge":
                relations.append(GraphEdgeRecord.model_validate(record["value"]))
            else:
                raise ValueError("graph projection artifact record type is invalid")
        return tuple(nodes), tuple(relations)

    async def _put(
        self,
        *,
        key: str,
        payload: bytes,
        kind: str,
        context: StorageOperationContext,
    ) -> ArtifactReference:
        return await self._writer.put(
            ImmutableArtifact(
                bucket=ARTIFACT_BUCKET,
                key=key,
                payload=payload,
                media_type="application/x-ndjson",
                artifact_kind=kind,
            ),
            context=context,
        )

    @staticmethod
    def _jsonl(values: Iterable[Mapping[str, object]]) -> bytes:
        records = tuple(dict(value) for value in values)
        for record in records:
            reject_runtime_fields(record)
        return b"".join(
            json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
            for record in records
        )

    @staticmethod
    def _load_jsonl(payload: bytes) -> tuple[dict[str, object], ...]:
        records: list[dict[str, object]] = []
        for line in payload.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("projection artifact JSONL record is invalid")
            records.append(record)
        for record in records:
            reject_runtime_fields(record)
        return tuple(records)
