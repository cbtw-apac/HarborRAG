"""Object-store repositories for canonical documents and replayable projections.

Artifacts are immutable. A reader brings records written by older releases to the
current shape instead of rewriting the stored artifact.
"""

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
    canonical_document_bytes,
    load_canonical_document,
    reject_runtime_fields,
)
from harborrag_core.storage import StorageOperationContext

_RETIRED_PAYLOAD_KEYS = frozenset({"preview", "content_reference"})
"""Payload keys older releases wrote and the current payload model no longer has."""


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
        adopt_existing: bool = False,
    ) -> ArtifactReference:
        """Store one canonical document, or adopt the identical one already stored."""
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
            adopt_existing=adopt_existing,
        )

    async def get(
        self,
        reference: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> Document:
        """Reload the canonical document an artifact reference points to."""
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
        """Store a version's canonical relations as JSONL, sorted by relation ID."""
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
        points: Sequence[VectorEvidenceRecord],
        context: StorageOperationContext,
    ) -> ArtifactReference:
        """Store a version's vector points as JSONL, sorted by point ID."""
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
        """Store a version's graph nodes, then its edges, as one JSONL artifact."""
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
        """Store one encoder profile's chunk representations for a version."""
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
        """Store a representation set under its own document and version."""
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
        """Reload the representation set an artifact reference points to."""
        return RepresentationSet.model_validate_json(
            await self._reader.get(reference, context=context)
        )

    async def get_vector_projection(
        self,
        reference: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> tuple[VectorEvidenceRecord, ...]:
        """Reload a version's vector points, migrating those older releases wrote."""
        values = self._load_jsonl(await self._reader.get(reference, context=context))
        return tuple(
            VectorEvidenceRecord.model_validate(record)
            for record in map(_current_vector_record, values)
            if record is not None
        )

    async def get_graph_projection(
        self,
        reference: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> tuple[tuple[GraphNodeRecord, ...], tuple[GraphEdgeRecord, ...]]:
        """Reload a version's graph nodes and edges, dropping retired fields."""
        nodes: list[GraphNodeRecord] = []
        relations: list[GraphEdgeRecord] = []
        for record in self._load_jsonl(await self._reader.get(reference, context=context)):
            record_type = record.get("record_type")
            if record_type not in {"node", "edge"}:
                raise ValueError("graph projection artifact record type is invalid")
            value = record.get("value")
            if not isinstance(value, Mapping):
                raise ValueError("graph projection artifact record value is invalid")
            value = dict(value)
            if record_type == "node":
                value.pop("content_preview", None)
                nodes.append(GraphNodeRecord.model_validate(value))
            else:
                value.pop("evidence_chunk_ids", None)
                relations.append(GraphEdgeRecord.model_validate(value))
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


def _current_vector_record(value: dict[str, object]) -> dict[str, object] | None:
    """Bring one stored vector point to the current shape, or None to skip it.

    Older releases wrote route points, which are no longer projected anywhere, so
    replaying their artifacts skips them rather than failing to parse. They also
    wrote retired payload keys, which are dropped, and ``chunk_kind`` as one string,
    which becomes a one-item list.
    """
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        return value
    if payload.get("record_kind") == "route":
        return None
    migrated = {key: item for key, item in payload.items() if key not in _RETIRED_PAYLOAD_KEYS}
    chunk_kind = migrated.get("chunk_kind")
    if isinstance(chunk_kind, str):
        migrated["chunk_kind"] = [chunk_kind]
    return {**value, "payload": migrated}
