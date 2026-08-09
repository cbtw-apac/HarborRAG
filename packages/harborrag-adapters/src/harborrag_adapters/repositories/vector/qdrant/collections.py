from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from harborrag_adapters.repositories.errors import HarborStorageValidationError
from harborrag_adapters.repositories.telemetry import traced_repository_operation
from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
from harborrag_adapters.repositories.vector.qdrant.mapping import QdrantMapper
from harborrag_adapters.repositories.vector.qdrant.query import QdrantQueryExecutor
from harborrag_core.indexing import VectorIndexSpec
from harborrag_core.storage import StorageOperationContext

try:
    from qdrant_client import models as qm
except ImportError:  # pragma: no cover - optional dependency
    qm = None  # type: ignore[assignment]


class QdrantCollectionMixin:
    """Create and validate tenant-scoped Qdrant collections."""

    _database: QdrantDBClient
    _queries: QdrantQueryExecutor
    _specs: dict[tuple[str, str], VectorIndexSpec]
    _collection_locks: dict[tuple[str, str], asyncio.Lock]

    @traced_repository_operation("ensure_index")
    async def ensure_index(
        self,
        spec: VectorIndexSpec,
        *,
        context: StorageOperationContext,
    ) -> None:
        if not spec.tenant_scoped:
            raise HarborStorageValidationError(
                "the Qdrant adapter requires tenant_scoped=True",
                context=self._queries.error_context(
                    "ensure_index", spec.index_name, context=context
                ),
            )
        key = self._queries.spec_key(spec.index_name, context)
        lock = self._collection_locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._ensure_collection(spec, context=context)

    async def _ensure_collection(
        self,
        spec: VectorIndexSpec,
        *,
        context: StorageOperationContext,
    ) -> None:
        name = self._queries.collection_name(spec.index_name, context)
        client = self._database.raw
        if not await client.collection_exists(name):
            try:
                await client.create_collection(**self._collection_options(name, spec))
            except Exception:
                if not await client.collection_exists(name):
                    raise
            else:
                for field in spec.metadata_indexes:
                    await client.create_payload_index(
                        collection_name=name,
                        field_name=field,
                        field_schema=self._payload_schema(field),
                        wait=True,
                    )
                self._specs[self._queries.spec_key(spec.index_name, context)] = spec
                return
        await self._validate_collection(name, spec, context)

    async def _validate_collection(
        self,
        name: str,
        spec: VectorIndexSpec,
        context: StorageOperationContext,
    ) -> None:
        persisted = await self._queries.require_spec(spec.index_name, context)
        persisted_shape = (
            persisted.dimension,
            persisted.distance,
            persisted.dense_vector_name,
            persisted.sparse_vector_name,
            persisted.sparse_idf,
        )
        requested_shape = (
            spec.dimension,
            spec.distance,
            spec.dense_vector_name,
            spec.sparse_vector_name,
            spec.sparse_idf,
        )
        if persisted_shape != requested_shape:
            raise HarborStorageValidationError(
                f"index {spec.index_name!r} already exists with another vector schema",
                context=self._queries.error_context("ensure_index", spec.index_name),
            )
        info = await self._database.raw.get_collection(name)
        existing_indexes = getattr(info, "payload_schema", {}) or {}
        for field in spec.metadata_indexes:
            existing = existing_indexes.get(field)
            if existing is not None and self._payload_schema_matches(field, existing):
                continue
            if existing is not None:
                await self._database.raw.delete_payload_index(
                    collection_name=name, field_name=field, wait=True
                )
            await self._database.raw.create_payload_index(
                collection_name=name,
                field_name=field,
                field_schema=self._payload_schema(field),
                wait=True,
            )
        self._specs[self._queries.spec_key(spec.index_name, context)] = spec

    @staticmethod
    def _collection_options(
        physical_name: str,
        spec: VectorIndexSpec,
    ) -> dict[str, Any]:
        dense = qm.VectorParams(
            size=spec.dimension,
            distance=QdrantMapper.distance(spec.distance, qm),
        )
        options: dict[str, Any] = {"collection_name": physical_name}
        if spec.dense_vector_name is None:
            options["vectors_config"] = dense
            return options
        options["vectors_config"] = {spec.dense_vector_name: dense}
        if spec.sparse_vector_name is not None:
            options["sparse_vectors_config"] = {
                spec.sparse_vector_name: qm.SparseVectorParams(
                    modifier=qm.Modifier.IDF if spec.sparse_idf else None
                )
            }
        return options

    @staticmethod
    def _payload_schema(field: str) -> Any:
        del field
        return qm.PayloadSchemaType.KEYWORD

    @classmethod
    def _payload_schema_matches(cls, field: str, existing: object) -> bool:
        current = (
            existing.get("data_type")
            if isinstance(existing, Mapping)
            else getattr(existing, "data_type", None)
        )
        if current is None:
            return True
        expected = cls._payload_schema(field)
        return (
            str(getattr(current, "value", current)).lower()
            == str(getattr(expected, "value", expected)).lower()
        )

    @traced_repository_operation("index_exists")
    async def index_exists(
        self,
        name: str,
        *,
        context: StorageOperationContext,
    ) -> bool:
        physical_name = self._queries.collection_name(name, context)
        return bool(await self._database.raw.collection_exists(physical_name))

    @traced_repository_operation("delete_index")
    async def delete_index(
        self,
        name: str,
        *,
        context: StorageOperationContext,
    ) -> None:
        physical_name = self._queries.collection_name(name, context)
        if await self._database.raw.collection_exists(physical_name):
            await self._database.raw.delete_collection(collection_name=physical_name)
        self._specs.pop(self._queries.spec_key(name, context), None)
