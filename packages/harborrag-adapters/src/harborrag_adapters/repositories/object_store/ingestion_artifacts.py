from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageNotFoundError,
)
from harborrag_adapters.repositories.object_store.base import HarborObjectStore
from harborrag_core.chunking import ConnectorType
from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import ArtifactReference
from harborrag_core.invariants import HarborInvariantError
from harborrag_core.schemas.object_store import PutObjectRequest
from harborrag_core.storage import StorageOperationContext

RAW_BUCKET = "harborrag-raw"
ARTIFACT_BUCKET = "harborrag-artifacts"


@dataclass(frozen=True, slots=True)
class ImmutableArtifact:
    bucket: str
    key: str
    payload: bytes
    media_type: str
    artifact_kind: str


class IngestionArtifactLayout:
    """Build version-addressed logical object keys without runtime identifiers."""

    @staticmethod
    def raw(connector: ConnectorType, document_id: str, content_hash: str) -> str:
        return f"raw/{connector.value}/{document_id}/{content_hash}/source"

    @staticmethod
    def raw_metadata(
        connector: ConnectorType,
        document_id: str,
        content_hash: str,
        metadata_hash: str,
    ) -> str:
        return f"raw/{connector.value}/{document_id}/{content_hash}/metadata/{metadata_hash}.json"

    @staticmethod
    def parsed(document_id: str, parser_fingerprint: str, content_hash: str) -> str:
        return f"parsed/{document_id}/{parser_fingerprint}/{content_hash}.json"

    @staticmethod
    def canonical(document_id: str, document_version_id: str) -> str:
        return f"canonical/{document_id}/{document_version_id}.json"

    @staticmethod
    def comments(document_id: str, document_version_id: str) -> str:
        return f"comments/{document_id}/{document_version_id}.json"

    @staticmethod
    def relations(document_id: str, document_version_id: str) -> str:
        return f"relations/{document_id}/{document_version_id}.json"

    @staticmethod
    def chunks(document_id: str, document_version_id: str) -> str:
        return f"chunks/{document_id}/{document_version_id}.jsonl"

    @staticmethod
    def chunk_index(document_id: str, document_version_id: str) -> str:
        return f"chunks/{document_id}/{document_version_id}.idx"

    @staticmethod
    def table(document_id: str, document_version_id: str, table_id: str) -> str:
        return f"tables/{document_id}/{document_version_id}/{table_id}.parquet"

    @staticmethod
    def representation(
        document_id: str,
        document_version_id: str,
        encoder_profile: str,
    ) -> str:
        return f"representations/{document_id}/{document_version_id}/{encoder_profile}.bin"

    @staticmethod
    def vector_projection(document_id: str, document_version_id: str) -> str:
        return f"projections/{document_id}/{document_version_id}/vector.jsonl"

    @staticmethod
    def graph_projection(document_id: str, document_version_id: str) -> str:
        return f"projections/{document_id}/{document_version_id}/graph.jsonl"

    @staticmethod
    def source_plan(task_id: str, scan_id: str) -> str:
        return f"source-plans/{task_id}/{scan_id}.json"

    @staticmethod
    def source_plan_page(task_id: str, scan_id: str, page_number: int) -> str:
        if page_number < 0:
            raise ValueError("source plan page number must not be negative")
        return f"source-plans/{task_id}/{scan_id}/pages/{page_number:08d}.json"


class ImmutableArtifactWriter:
    """Write immutable artifacts and make identical activity retries a no-op."""

    def __init__(self, store: HarborObjectStore) -> None:
        self._store = store

    async def put(
        self,
        artifact: ImmutableArtifact,
        *,
        context: StorageOperationContext,
    ) -> ArtifactReference:
        checksum = sha256(artifact.payload).hexdigest()
        request = PutObjectRequest(
            bucket=artifact.bucket,
            key=artifact.key,
            body=artifact.payload,
            content_type=artifact.media_type,
            checksum_sha256=checksum,
            if_none_match=True,
            metadata={"artifact_kind": artifact.artifact_kind, "sha256": checksum},
        )
        try:
            stored = await self._store.put(request, context=context)
        except HarborStorageAlreadyExistsError:
            return await self._resolve_replay(
                bucket=artifact.bucket,
                key=artifact.key,
                checksum=checksum,
                media_type=artifact.media_type,
                context=context,
            )
        return ArtifactReference(
            bucket=artifact.bucket,
            key=artifact.key,
            sha256=checksum,
            byte_size=stored.size_bytes,
            media_type=artifact.media_type,
        )

    async def _resolve_replay(
        self,
        *,
        bucket: str,
        key: str,
        checksum: str,
        media_type: str,
        context: StorageOperationContext,
    ) -> ArtifactReference:
        metadata = await self._store.head(bucket, key, context=context)
        existing_checksum = metadata.metadata.get("sha256") or metadata.reference.checksum_sha256
        if existing_checksum != checksum:
            raise HarborConflictError(
                f"immutable artifact key already contains different content: {bucket}/{key}"
            )
        return ArtifactReference(
            bucket=bucket,
            key=key,
            sha256=checksum,
            byte_size=metadata.reference.size_bytes,
            media_type=metadata.reference.content_type or media_type,
        )


class ImmutableArtifactReader:
    """Read complete immutable artifacts or one validated byte range."""

    def __init__(self, store: HarborObjectStore) -> None:
        self._store = store

    async def get(
        self,
        reference: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> bytes:
        byte_range = None
        if reference.byte_offset is not None:
            if reference.byte_length is None:
                raise HarborInvariantError("reference.byte_length must not be None here")
            byte_range = (
                reference.byte_offset,
                reference.byte_offset + reference.byte_length - 1,
            )
        return await self._store.get_bytes(
            reference.bucket,
            reference.key,
            byte_range=byte_range,
            context=context,
        )

    async def get_range(
        self,
        *,
        bucket: str,
        object_key: str,
        byte_offset: int,
        byte_length: int,
        context: StorageOperationContext,
    ) -> bytes:
        if byte_offset < 0 or byte_length < 1:
            raise ValueError("artifact byte range must be positive")
        return await self._store.get_bytes(
            bucket,
            object_key,
            byte_range=(byte_offset, byte_offset + byte_length - 1),
            context=context,
        )

    async def find(
        self,
        *,
        bucket: str,
        key: str,
        media_type: str,
        context: StorageOperationContext,
    ) -> ArtifactReference | None:
        """Resolve an immutable artifact reference without loading its bytes."""

        try:
            metadata = await self._store.head(bucket, key, context=context)
        except HarborStorageNotFoundError:
            return None
        checksum = metadata.metadata.get("sha256") or metadata.reference.checksum_sha256
        if checksum is None:
            raise HarborConflictError(f"immutable artifact is missing its checksum: {bucket}/{key}")
        return ArtifactReference(
            bucket=bucket,
            key=key,
            sha256=checksum,
            byte_size=metadata.reference.size_bytes,
            media_type=metadata.reference.content_type or media_type,
        )
