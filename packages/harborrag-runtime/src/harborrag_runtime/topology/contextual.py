"""Independent, frozen contextual vectors; never overwrite baseline evidence points."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from harborrag_adapters.repositories.object_store import (
    ARTIFACT_BUCKET,
    ImmutableArtifact,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
)
from harborrag_core.indexing import VectorIndexRecord
from harborrag_core.ingestion import ArtifactReference
from harborrag_core.models.embed import (
    EmbeddingPurpose,
    HarborEmbedMetadata,
    HarborEmbedRequest,
    HarborEmbedResponse,
)
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import (
    ChunkExtractionInput,
    ExtractionOutput,
    TopologyJob,
)
from harborrag_core.topology.derived import (
    ChunkEnrichment,
    ContextualIndexProfile,
    ContextualManifest,
)
from harborrag_engine.topology.contextual_builder import ContextualRecordBuilder


class Embedder(Protocol):
    async def aembed(self, request: HarborEmbedRequest) -> HarborEmbedResponse: ...


@dataclass(frozen=True)
class ContextualResources:
    embed: Embedder
    writer: ImmutableArtifactWriter
    reader: ImmutableArtifactReader


class ContextualMaterializer:
    def __init__(
        self,
        resources: ContextualResources,
        profile: ContextualIndexProfile,
    ) -> None:
        self._resources = resources
        self._profile = profile
        self._builder = ContextualRecordBuilder(profile)

    async def materialize(
        self,
        job: TopologyJob,
        build_id: str,
        inputs: tuple[ChunkExtractionInput, ...],
        outputs: Mapping[str, ExtractionOutput | ChunkEnrichment],
    ) -> ContextualManifest | None:
        if set(outputs) != {value.chunk_id for value in inputs}:
            raise ValueError("contextual output coverage does not match source chunks")
        if any(not output.description.strip() for output in outputs.values()):
            raise ValueError("contextual output is incomplete")
        selected = [
            (value, outputs[value.chunk_id])
            for value in inputs
            if outputs[value.chunk_id].description.strip()
        ]
        if not selected:
            return None
        context = StorageOperationContext.system(job.tenant_id)
        key = f"topology/contextual/{build_id}/{self._profile.fingerprint}.json"
        existing = await self._resources.reader.find(
            bucket=ARTIFACT_BUCKET, key=key, media_type="application/json", context=context
        )
        if existing is not None:
            records = await read_contextual_records(self._resources.reader, existing, context)
        else:
            records = []
            for value, output in selected:
                # One input per request bounds memory and preserves source-to-vector alignment.
                response = await self._resources.embed.aembed(
                    HarborEmbedRequest(
                        inputs=(self._builder.text(value, output.description),),
                        logical_model=self._profile.model,
                        dimensions=self._profile.dimension,
                        purpose=EmbeddingPurpose.DOCUMENT,
                        cacheable=False,
                        normalize=True,
                        sensitive=True,
                        metadata=HarborEmbedMetadata(
                            tenant_id=job.tenant_id,
                            document_ids=(job.document_id,),
                            chunk_ids=(value.chunk_id,),
                        ),
                    )
                )
                if len(response.embeddings) != 1 or not isinstance(
                    response.embeddings[0].value, tuple
                ):
                    raise ValueError("contextual embedding response is not a single float vector")
                vector = response.embeddings[0].value
                records.append(self._builder.build(job, build_id, value, vector))
            existing = await self._resources.writer.put(
                ImmutableArtifact(
                    bucket=ARTIFACT_BUCKET,
                    key=key,
                    payload=json.dumps(
                        [record.model_dump(mode="json") for record in records], sort_keys=True
                    ).encode(),
                    media_type="application/json",
                    artifact_kind="contextual-vectors",
                ),
                context=context,
            )
        return ContextualManifest(
            artifact=existing,
            embedding_profile=self._profile.fingerprint,
            dimension=self._profile.dimension,
            point_ids=tuple(record.id for record in records),
            index_name=self._profile.index_name,
        )


async def read_contextual_records(
    reader: ImmutableArtifactReader,
    reference: ArtifactReference,
    context: StorageOperationContext,
) -> list[VectorIndexRecord]:
    payload = await reader.get(reference, context=context)
    if len(payload) != reference.byte_size or sha256(payload).hexdigest() != reference.sha256:
        raise ValueError("contextual vector artifact integrity failure")
    return [VectorIndexRecord.model_validate(row) for row in json.loads(payload)]
