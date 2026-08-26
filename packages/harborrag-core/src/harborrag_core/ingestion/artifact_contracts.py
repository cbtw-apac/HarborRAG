from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from .source_contracts import ChangeFingerprints, SourceIdentity
from .states import DocumentVersionState


class ArtifactReference(StrictModel):
    """Reference immutable content without carrying its bytes through workflows."""

    bucket: str = Field(min_length=1)
    key: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0)
    media_type: str = Field(min_length=1)
    byte_offset: int | None = Field(default=None, ge=0)
    byte_length: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> ArtifactReference:
        if (self.byte_offset is None) != (self.byte_length is None):
            raise ValueError("artifact byte_offset and byte_length must be set together")
        if self.byte_offset is not None:
            assert self.byte_length is not None
            if self.byte_offset + self.byte_length <= self.byte_size:
                return self
            raise ValueError("artifact byte range exceeds the artifact size")
        return self


class ContentReference(StrictModel):
    """Exact byte range for one chunk in a durable JSONL object."""

    bucket: str = Field(min_length=1)
    object_key: str = Field(min_length=1)
    byte_offset: int = Field(ge=0)
    byte_length: int = Field(gt=0)


class RawDocumentReference(StrictModel):
    """Durable raw bytes plus the connector metadata needed for replay."""

    document_id: DocumentId
    connector_type: str = Field(min_length=1)
    content_hash: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    source_artifact: ArtifactReference
    metadata_artifact: ArtifactReference


class ChunkIndexEntry(StrictModel):
    """Locate one canonical chunk inside an immutable JSONL artifact."""

    chunk_id: str = Field(min_length=1)
    byte_offset: int = Field(ge=0)
    byte_length: int = Field(gt=0)


class ChunkSetArtifacts(StrictModel):
    chunks: ArtifactReference
    index: ArtifactReference
    entries: tuple[ChunkIndexEntry, ...]

    @model_validator(mode="after")
    def validate_entries(self) -> ChunkSetArtifacts:
        chunk_ids = tuple(entry.chunk_id for entry in self.entries)
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("chunk index entries must have unique chunk IDs")
        previous_end = 0
        for entry in self.entries:
            if entry.byte_offset < previous_end:
                raise ValueError("chunk index entries must be ordered and non-overlapping")
            if entry.byte_offset + entry.byte_length > self.chunks.byte_size:
                raise ValueError("chunk index entry exceeds the JSONL artifact size")
            previous_end = entry.byte_offset + entry.byte_length
        return self

    def content_reference(self, chunk_id: str) -> ContentReference:
        entry = next(
            (candidate for candidate in self.entries if candidate.chunk_id == chunk_id),
            None,
        )
        if entry is None:
            raise KeyError(f"chunk is not present in the chunk index: {chunk_id}")
        return ContentReference(
            bucket=self.chunks.bucket,
            object_key=self.chunks.key,
            byte_offset=entry.byte_offset,
            byte_length=entry.byte_length,
        )


class DocumentVersionCandidate(StrictModel):
    """Version-addressed document candidate persisted before publication."""

    document_id: DocumentId
    document_version_id: DocumentVersionId
    source_identity: SourceIdentity
    fingerprints: ChangeFingerprints
    state: DocumentVersionState = DocumentVersionState.PENDING
    canonical_artifact: ArtifactReference | None = None
    created_at: datetime | None = None
