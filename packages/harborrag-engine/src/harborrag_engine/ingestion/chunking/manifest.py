from __future__ import annotations

from typing import Protocol

from harborrag_core.schemas.documents import ChunkRecord

from .schemas import ChunkingResult, ChunkManifest


class CanonicalChunkRepository(Protocol):
    """Persist canonical chunk bodies before their manifest is activated."""

    async def put(self, records: tuple[ChunkRecord, ...]) -> None:
        """Persist immutable chunk revisions idempotently."""

    async def get_many(
        self,
        tenant_id: str,
        chunk_revision_ids: tuple[str, ...],
    ) -> tuple[ChunkRecord, ...]:
        """Load immutable bodies referenced by a manifest for reindexing."""


class ChunkManifestRepository(Protocol):
    """Persist and retrieve lightweight chunk manifests."""

    async def put(self, manifest: ChunkManifest) -> None:
        """Persist one validated manifest idempotently."""

    async def get(
        self,
        tenant_id: str,
        artifact_id: str,
        artifact_revision_id: str,
        configuration_hash: str,
    ) -> ChunkManifest | None:
        """Load a tenant-scoped manifest for restart or reindexing."""


class ChunkPersistenceService:
    """Persist pure chunking output without coupling storage to chunk creation."""

    def __init__(
        self,
        chunks: CanonicalChunkRepository,
        manifests: ChunkManifestRepository,
    ) -> None:
        self._chunks = chunks
        self._manifests = manifests

    async def persist(self, result: ChunkingResult) -> None:
        """Persist immutable bodies before exposing their validated manifest."""

        if not result.manifest.validation.valid:
            raise ValueError("cannot persist an invalid chunk manifest")
        await self._chunks.put(result.chunks)
        await self._manifests.put(result.manifest)
