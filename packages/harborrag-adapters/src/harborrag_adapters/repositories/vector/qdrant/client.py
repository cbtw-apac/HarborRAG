from __future__ import annotations

import math
from typing import Any

from harborrag_adapters.repositories.lifecycle import AsyncLifecycle
from harborrag_adapters.repositories.vector.qdrant.config import QdrantDeployment

AsyncQdrantClient: Any
try:
    from qdrant_client import AsyncQdrantClient as _AsyncQdrantClient
except ImportError:  # pragma: no cover - optional dependency
    AsyncQdrantClient = None
else:
    AsyncQdrantClient = _AsyncQdrantClient


class QdrantDBClient(AsyncLifecycle):
    """Owns a real Qdrant client in embedded-memory, embedded-disk, or remote form."""

    def __init__(
        self,
        *,
        deployment: QdrantDeployment,
        url: str | None,
        path: str | None,
        api_key: str | None,
        prefer_grpc: bool,
        operation_timeout_seconds: float,
    ) -> None:
        if AsyncQdrantClient is None:
            raise ImportError("qdrant-client is not installed")
        self._deployment = deployment
        self._url = url
        self._path = path
        self._api_key = api_key
        self._prefer_grpc = prefer_grpc
        self._operation_timeout_seconds = operation_timeout_seconds
        self._client: Any = None

    @property
    def backend(self) -> str:
        return "qdrant"

    @property
    def deployment(self) -> QdrantDeployment:
        return self._deployment

    @property
    def storage(self) -> str:
        if self._deployment is QdrantDeployment.REMOTE:
            return "remote"
        return "disk" if self._path else "memory"

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def raw(self) -> Any:
        if self._client is None:
            raise RuntimeError("Qdrant database client is not connected")
        return self._client

    async def connect(self) -> None:
        if self._client is not None:
            return
        try:
            if self._deployment is QdrantDeployment.EMBEDDED:
                if self._path:
                    self._client = AsyncQdrantClient(path=self._path)
                else:
                    self._client = AsyncQdrantClient(location=":memory:")
            else:
                self._client = AsyncQdrantClient(
                    url=self._url,
                    api_key=self._api_key,
                    prefer_grpc=self._prefer_grpc,
                    timeout=max(1, math.ceil(self._operation_timeout_seconds)),
                )
            await self.ping()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def ping(self) -> None:
        await self.raw.get_collections()
