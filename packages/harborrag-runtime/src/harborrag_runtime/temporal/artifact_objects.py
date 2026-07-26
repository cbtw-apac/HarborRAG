"""Object-store repositories for durable ingestion artifacts."""

from __future__ import annotations

from hashlib import sha256
from urllib.parse import quote, unquote, urlsplit

from harborrag_adapters.repositories.object_store.base import HarborObjectStore
from harborrag_core.schemas.documents import ChunkRecord
from harborrag_core.schemas.ids import TenantId
from harborrag_core.schemas.object_store import PutObjectRequest
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_engine.ingestion.chunking.manifest import (
    CanonicalChunkRepository,
    ChunkManifestRepository,
)
from harborrag_engine.ingestion.chunking.schemas import ChunkManifest

from .ingestioncodec import dump_payload, load_chunk_manifest, load_payload

_BUCKET = "ingestion"


class IngestionObjectRepository:
    """Store typed ingestion payloads behind compact, tenant-bearing references."""

    def __init__(self, store: HarborObjectStore) -> None:
        self.store = store

    async def put(
        self,
        tenant_id: str,
        key: str,
        payload: bytes,
        *,
        kind: str,
    ) -> str:
        context = self.context(tenant_id)
        await self.store.put(
            PutObjectRequest(
                bucket=_BUCKET,
                key=key,
                body=payload,
                content_type="application/json",
                metadata={"kind": kind},
            ),
            context=context,
        )
        return self.reference(tenant_id, key)

    async def get(
        self,
        reference: str,
        *,
        expected_tenant_id: str,
        expected_bucket: str = _BUCKET,
        expected_key_prefix: str | None = None,
        expected_key_suffix: str | None = None,
    ) -> bytes:
        tenant_id, bucket, key = self.parts(reference)
        if tenant_id != expected_tenant_id:
            raise ValueError("ingestion object tenant does not match activity tenant")
        if bucket != expected_bucket:
            raise ValueError("ingestion object bucket is not authorized")
        if expected_key_prefix is not None and not key.startswith(expected_key_prefix):
            raise ValueError("ingestion object key kind is not authorized")
        if expected_key_suffix is not None and not key.endswith(expected_key_suffix):
            raise ValueError("ingestion object key kind is not authorized")
        return await self.store.get_bytes(
            bucket,
            key,
            byte_range=None,
            context=self.context(expected_tenant_id),
        )

    async def exists(self, tenant_id: str, key: str) -> bool:
        return await self.store.exists(
            _BUCKET,
            key,
            context=self.context(tenant_id),
        )

    @staticmethod
    def context(tenant_id: str) -> StorageOperationContext:
        return StorageOperationContext(tenant_id=TenantId(tenant_id))

    @staticmethod
    def reference(tenant_id: str, key: str) -> str:
        return f"harbor-object://{quote(tenant_id, safe='')}/{_BUCKET}/{key}"

    @staticmethod
    def parts(reference: str) -> tuple[str, str, str]:
        parsed = urlsplit(reference)
        path = parsed.path.lstrip("/").split("/", 1)
        if parsed.scheme != "harbor-object" or not parsed.netloc or len(path) != 2:
            raise ValueError(f"invalid ingestion object reference: {reference!r}")
        return unquote(parsed.netloc), path[0], path[1]


class ObjectChunkRepository(CanonicalChunkRepository):
    """Persist immutable canonical chunk bodies in the configured object store."""

    def __init__(self, objects: IngestionObjectRepository) -> None:
        self._objects = objects

    async def put(self, records: tuple[ChunkRecord, ...]) -> None:
        for record in records:
            key = self._key(str(record.chunk_revision_id))
            await self._objects.put(
                str(record.tenant_id),
                key,
                dump_payload("canonical-chunk", record),
                kind="canonical-chunk",
            )

    async def get_many(
        self,
        tenant_id: str,
        chunk_revision_ids: tuple[str, ...],
    ) -> tuple[ChunkRecord, ...]:
        records: list[ChunkRecord] = []
        for revision_id in chunk_revision_ids:
            reference = self._objects.reference(tenant_id, self._key(revision_id))
            value = load_payload(
                await self._objects.get(
                    reference,
                    expected_tenant_id=tenant_id,
                    expected_key_prefix="chunks/",
                    expected_key_suffix=".json",
                ),
                "canonical-chunk",
            )
            records.append(ChunkRecord.model_validate(value))
        return tuple(records)

    @staticmethod
    def _key(revision_id: str) -> str:
        return f"chunks/{digest(revision_id)}.json"


class ObjectManifestRepository(ChunkManifestRepository):
    """Persist chunk manifests independently from their canonical bodies."""

    def __init__(self, objects: IngestionObjectRepository) -> None:
        self._objects = objects

    async def put(self, manifest: ChunkManifest) -> None:
        await self._objects.put(
            manifest.tenant_id,
            self._key(
                manifest.artifact_id,
                manifest.artifact_revision_id,
                manifest.configuration_hash,
            ),
            dump_payload("chunk-manifest", manifest),
            kind="chunk-manifest",
        )

    async def get(
        self,
        tenant_id: str,
        artifact_id: str,
        artifact_revision_id: str,
        configuration_hash: str,
    ) -> ChunkManifest | None:
        key = self._key(artifact_id, artifact_revision_id, configuration_hash)
        if not await self._objects.exists(tenant_id, key):
            return None
        reference = self._objects.reference(tenant_id, key)
        value = load_payload(
            await self._objects.get(
                reference,
                expected_tenant_id=tenant_id,
                expected_key_prefix="manifests/",
                expected_key_suffix=".json",
            ),
            "chunk-manifest",
        )
        return load_chunk_manifest(value)

    @staticmethod
    def _key(artifact_id: str, revision_id: str, configuration_hash: str) -> str:
        identity = f"{artifact_id}\0{revision_id}\0{configuration_hash}"
        return f"manifests/{digest(identity)}.json"


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
