"""Freeze extraction results and check integrity before reuse or reprojection."""

from __future__ import annotations

from hashlib import sha256

from harborrag_adapters.repositories.object_store.ingestion_artifacts import (
    ARTIFACT_BUCKET,
    ImmutableArtifact,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
)
from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import ArtifactReference
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.extraction import (
    ChunkExtractionInput,
    ExtractionOutput,
    ExtractionProfile,
)


class ExtractionArtifacts:
    def __init__(self, writer: ImmutableArtifactWriter, reader: ImmutableArtifactReader) -> None:
        self._writer = writer
        self._reader = reader

    @staticmethod
    def key(profile: ExtractionProfile, value: ChunkExtractionInput) -> str:
        return f"topology/extractions/{profile.fingerprint}/{value.input_digest}.json"

    async def find(
        self,
        profile: ExtractionProfile,
        value: ChunkExtractionInput,
        *,
        context: StorageOperationContext,
    ) -> ArtifactReference | None:
        return await self._reader.find(
            bucket=ARTIFACT_BUCKET,
            key=self.key(profile, value),
            media_type="application/json",
            context=context,
        )

    async def read(
        self,
        reference: ArtifactReference,
        value: ChunkExtractionInput,
        *,
        context: StorageOperationContext,
        profile: ExtractionProfile,
    ) -> ExtractionOutput:
        if (
            reference.bucket != ARTIFACT_BUCKET
            or reference.key != self.key(profile, value)
            or reference.media_type != "application/json"
            or reference.byte_offset is not None
        ):
            raise ValueError("topology artifact reference does not match extraction profile/input")
        payload = await self._reader.get(reference, context=context)
        if sha256(payload).hexdigest() != reference.sha256 or len(payload) != reference.byte_size:
            raise ValueError("topology extraction artifact integrity check failed")
        result = ExtractionOutput.model_validate_json(payload)
        result.validate_evidence(value)
        result.validate_profile(profile)
        return result

    async def freeze(
        self,
        profile: ExtractionProfile,
        value: ChunkExtractionInput,
        output: ExtractionOutput,
        *,
        context: StorageOperationContext,
    ) -> ArtifactReference:
        output.validate_evidence(value)
        output.validate_profile(profile)
        try:
            return await self._writer.put(
                ImmutableArtifact(
                    bucket=ARTIFACT_BUCKET,
                    key=self.key(profile, value),
                    payload=output.model_dump_json().encode(),
                    media_type="application/json",
                    artifact_kind="topology-extraction",
                ),
                context=context,
            )
        except HarborConflictError:
            # A competing worker may have frozen a different valid response first.
            # Reuse that accepted artifact; never overwrite a successful extraction.
            reference = await self.find(profile, value, context=context)
            if reference is None:
                raise
            await self.read(reference, value, context=context, profile=profile)
            return reference

    async def manifest(
        self, build_id: str, payload: bytes, *, context: StorageOperationContext
    ) -> ArtifactReference:
        return await self._writer.put(
            ImmutableArtifact(
                bucket=ARTIFACT_BUCKET,
                key=f"topology/builds/{build_id}.json",
                payload=payload,
                media_type="application/json",
                artifact_kind="topology-build",
            ),
            context=context,
        )
