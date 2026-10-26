"""Frozen parent vector views with all-input lineage and narrower citation maps."""

import json
from dataclasses import dataclass
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from harborrag_adapters.repositories.object_store import ARTIFACT_BUCKET, ImmutableArtifact
from harborrag_core.indexing import VectorIndexRecord
from harborrag_core.ingestion import ArtifactReference
from harborrag_core.models.embed import EmbeddingPurpose, HarborEmbedMetadata, HarborEmbedRequest
from harborrag_core.schemas.ids import TenantId
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import DocumentTopologyBuild, TopologyJob
from harborrag_core.topology.derived import (
    ContextualIndexProfile,
    ContextualManifest,
    ParentDescription,
)
from harborrag_core.topology.extraction import digest
from harborrag_engine.topology.vector_values import canonical_dense_vector

from .contextual import ContextualResources, read_contextual_records


@dataclass(frozen=True)
class ParentMaterializer:
    resources: ContextualResources
    profile: ContextualIndexProfile

    async def freeze_summaries(
        self,
        job: TopologyJob,
        build: DocumentTopologyBuild,
        parents: tuple[ParentDescription, ...],
    ) -> ArtifactReference:
        """Persist the generated summary product before either projection is attempted."""

        context = StorageOperationContext.system(job.tenant_id)
        payload = json.dumps(
            [parent.model_dump(mode="json") for parent in parents],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        content_digest = sha256(payload).hexdigest()
        key = (
            f"topology/parent-summaries/{build.build_id}/"
            f"{self.profile.parent_fingerprint}/{content_digest}.json"
        )
        reference = await self.resources.reader.find(
            bucket=ARTIFACT_BUCKET,
            key=key,
            media_type="application/json",
            context=context,
        )
        if reference is not None:
            existing = await self.resources.reader.get(reference, context=context)
            if sha256(existing).hexdigest() != content_digest:
                raise ValueError("parent summary artifact integrity failure")
            return reference
        return await self.resources.writer.put(
            ImmutableArtifact(
                bucket=ARTIFACT_BUCKET,
                key=key,
                payload=payload,
                media_type="application/json",
                artifact_kind="parent-descriptions",
            ),
            context=context,
        )

    async def materialize(
        self,
        job: TopologyJob,
        build: DocumentTopologyBuild,
        parents: tuple[ParentDescription, ...],
    ) -> ContextualManifest:
        context = StorageOperationContext.system(job.tenant_id)
        content_digest = digest([parent.model_dump(mode="json") for parent in parents])
        key = f"topology/parent-vectors/{build.build_id}/{self.profile.parent_fingerprint}/{content_digest}.json"
        reference = await self.resources.reader.find(
            bucket=ARTIFACT_BUCKET,
            key=key,
            media_type="application/json",
            context=context,
        )
        if reference is None:
            records = [await self._record(job, build, parent) for parent in parents]
            reference = await self.resources.writer.put(
                ImmutableArtifact(
                    bucket=ARTIFACT_BUCKET,
                    key=key,
                    payload=json.dumps(
                        [row.model_dump(mode="json") for row in records], sort_keys=True
                    ).encode(),
                    media_type="application/json",
                    artifact_kind="parent-description-vectors",
                ),
                context=context,
            )
        else:
            records = await read_contextual_records(self.resources.reader, reference, context)
        return ContextualManifest(
            artifact=reference,
            embedding_profile=self.profile.parent_fingerprint,
            dimension=self.profile.dimension,
            point_ids=tuple(row.id for row in records),
            index_name=self.profile.parent_index_name,
        )

    async def _record(
        self,
        job: TopologyJob,
        build: DocumentTopologyBuild,
        parent: ParentDescription,
    ) -> VectorIndexRecord:
        if len(parent.description.encode()) > self.profile.max_input_bytes:
            raise ValueError("parent description exceeds embedding input budget")
        response = await self.resources.embed.aembed(
            HarborEmbedRequest(
                inputs=(parent.description,),
                logical_model=self.profile.model,
                dimensions=self.profile.dimension,
                purpose=EmbeddingPurpose.DOCUMENT,
                cacheable=False,
                normalize=True,
                sensitive=True,
                metadata=HarborEmbedMetadata(
                    tenant_id=job.tenant_id, document_ids=(job.document_id,)
                ),
            )
        )
        if len(response.embeddings) != 1 or not isinstance(response.embeddings[0].value, tuple):
            raise ValueError("parent embedding is not a single dense vector")
        vector = canonical_dense_vector(response.embeddings[0].value)
        if len(vector) != self.profile.dimension:
            raise ValueError("parent embedding dimension mismatch")
        identity = str(
            uuid5(
                NAMESPACE_URL,
                f"{job.tenant_id}:{build.build_id}:parent:{parent.parent_key}:{self.profile.parent_fingerprint}",
            )
        )
        return VectorIndexRecord(
            id=identity,
            tenant_id=TenantId(job.tenant_id),
            vector=list(vector),
            payload={
                "record_kind": "parent_description",
                "build_id": build.build_id,
                "document_id": job.document_id,
                "document_version_id": job.document_version_id,
                "source_scope_id": job.source_scope_id,
                "config_epoch": job.config_epoch,
                "embedding_profile": self.profile.parent_fingerprint,
                "projection_point_id": identity,
                "parent_key": parent.parent_key,
                "level": parent.level,
                "section_path": list(parent.section_path),
                "structure_id": parent.structure_id,
                "chunk_ids": list(parent.input_chunk_ids),
                "cited_chunk_ids": list(parent.cited_chunk_ids),
                "description": parent.description,
                "input_coverage": parent.input_coverage,
                "semantic_coverage": parent.semantic_coverage,
            },
        )
