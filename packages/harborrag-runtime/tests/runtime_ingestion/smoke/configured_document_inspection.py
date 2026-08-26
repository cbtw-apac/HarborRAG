from __future__ import annotations

import asyncio
import json
from collections import Counter
from hashlib import sha256

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_adapters.repositories.graph.falkordb import (
    FalkorKnowledgeGraphRepository,
)
from harborrag_adapters.repositories.object_store import (
    CanonicalCommentArtifactRepository,
    CanonicalDocumentArtifactRepository,
    CanonicalTableArtifactRepository,
    ChunkArtifactReader,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
)
from harborrag_adapters.repositories.object_store.s3 import S3ObjectStore
from harborrag_adapters.repositories.vector import HarborVectorRepository
from harborrag_core.ingestion import canonical_document_bytes, reject_runtime_fields
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_runtime.composition.resources import (
    build_ingestion_control,
    build_knowledge_graph,
    build_object_store,
    build_vector_repository,
)
from harborrag_runtime.config.settings import RuntimeSettings


class ConfiguredStores:
    """Own the four stores inspected by one configured-source smoke run."""

    def __init__(
        self,
        *,
        control: IngestionControlPlaneDatabase,
        objects: S3ObjectStore,
        vectors: HarborVectorRepository,
        graph: FalkorKnowledgeGraphRepository,
    ) -> None:
        self.control = control
        self.objects = objects
        self.vectors = vectors
        self.graph = graph

    @classmethod
    def build(cls, settings: RuntimeSettings) -> ConfiguredStores:
        return cls(
            control=build_ingestion_control(settings),
            objects=build_object_store(settings),
            vectors=build_vector_repository(settings),
            graph=build_knowledge_graph(settings),
        )

    async def connect(self) -> None:
        for resource in (self.control, self.objects, self.vectors, self.graph):
            await resource.connect()

    async def close(self) -> None:
        await asyncio.gather(
            self.graph.close(),
            self.vectors.close(),
            self.objects.close(),
            self.control.close(),
            return_exceptions=True,
        )


async def inspect_documents(
    stores: ConfiguredStores,
    snapshots,
    context: StorageOperationContext,
) -> tuple[list[dict[str, object]], set[str]]:
    reader = ImmutableArtifactReader(stores.objects)
    writer = ImmutableArtifactWriter(stores.objects)
    canonical = CanonicalDocumentArtifactRepository(writer, reader)
    comments = CanonicalCommentArtifactRepository(writer, reader)
    tables = CanonicalTableArtifactRepository(writer, reader)
    chunk_reader = ChunkArtifactReader(reader)
    reports = []
    versions = set()
    for snapshot in snapshots:
        assert snapshot is not None
        assert snapshot.canonical_artifact is not None
        assert snapshot.chunk_artifact is not None
        versions.add(str(snapshot.document_version_id))
        document = await canonical.get(snapshot.canonical_artifact, context=context)
        reject_runtime_fields(json.loads(canonical_document_bytes(document)))
        chunks = await chunk_reader.get_all(snapshot.chunk_artifact, context=context)
        _assert_chunks(chunks)
        manifest = await stores.control.reliability.projection_manifest(
            str(snapshot.document_version_id)
        )
        if manifest is None or manifest.comment_artifact is None:
            raise AssertionError("configured smoke has no durable content manifest")
        comment_set = await comments.get(manifest.comment_artifact, context=context)
        if set(manifest.canonical_comment_ids) != {
            comment.comment_id for comment in comment_set.comments
        }:
            raise AssertionError("configured smoke comment artifact does not match manifest")
        canonical_table_ids = tuple(sorted(table.table_id for table in document.table_artifacts))
        if manifest.canonical_table_ids != canonical_table_ids:
            raise AssertionError("configured smoke table artifacts do not match canonical tables")
        table_shapes = []
        for reference in manifest.table_artifacts:
            rows = await tables.get_rows(reference, context=context)
            if not rows or not rows[0]:
                raise AssertionError("configured smoke Parquet table is empty")
            table_shapes.append((len(rows), len(rows[0])))
        reports.append(
            _document_report(
                snapshot=snapshot,
                document=document,
                chunks=chunks,
                comment_units=len(comment_set.comments),
                table_shapes=table_shapes,
            )
        )
    return reports, versions


def _assert_chunks(chunks) -> None:
    if not chunks:
        raise AssertionError("configured smoke canonical chunk set is empty")
    for chunk in chunks:
        reject_runtime_fields(chunk.model_dump(mode="json"))
        if not chunk.content.strip():
            raise AssertionError("configured smoke contains an empty chunk")
        if (
            chunk.chunk_kind.value == "table"
            and chunk.content.strip() == "Structured table reference"
        ):
            raise AssertionError("configured smoke table chunk contains placeholder evidence")


def _document_report(
    *,
    snapshot,
    document,
    chunks,
    comment_units: int,
    table_shapes: list[tuple[int, int]],
) -> dict[str, object]:
    return {
        "document_id_hash": _short_hash(str(snapshot.document_id)),
        "document_version_id_hash": _short_hash(str(snapshot.document_version_id)),
        "elements": len(document.content),
        "tables": len(document.table_artifacts),
        "relations": len(document.relations),
        "comment_units": _comment_count(document),
        "comment_artifact_units": comment_units,
        "parquet_table_shapes": table_shapes,
        "chunk_kinds": dict(sorted(Counter(chunk.chunk_kind.value for chunk in chunks).items())),
        "chunk_content": [
            {
                "kind": chunk.chunk_kind.value,
                "characters": len(chunk.content),
                "sha256": sha256(chunk.content.encode("utf-8")).hexdigest(),
            }
            for chunk in chunks
        ],
    }


def _comment_count(document) -> int:
    element_comments = sum(bool(element.metadata.get("comment_id")) for element in document.content)
    metadata_comments = document.provenance.extra.get("comments")
    return max(
        element_comments,
        len(metadata_comments) if isinstance(metadata_comments, (list, tuple)) else 0,
    )


def _short_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:16]
