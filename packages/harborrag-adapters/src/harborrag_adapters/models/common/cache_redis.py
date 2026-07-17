from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic import BaseModel

from .redis_client import RedisConnectionLifecycle


class PydanticResponseCodec:
    """Serialize only explicitly registered Pydantic response types for Redis storage."""

    def __init__(self, response_types: Mapping[str, type[BaseModel]] | None = None) -> None:
        """Register stable response identifiers accepted during cache decoding."""

        self._types = dict(response_types or default_response_types())
        self._names = {response_type: name for name, response_type in self._types.items()}

    def encode(self, value: BaseModel) -> str:
        """Encode a registered response without Python pickle or arbitrary imports."""

        name = self._names.get(type(value))
        if name is None:
            raise TypeError(f"unsupported cached response type: {type(value).__name__}")
        return json.dumps(
            {"schema": 1, "type": name, "value": value.model_dump(mode="json")},
            sort_keys=True,
            separators=(",", ":"),
        )

    def decode(self, payload: str | bytes) -> BaseModel:
        """Validate one encoded response against the registered stable type mapping."""

        raw = json.loads(payload.decode() if isinstance(payload, bytes) else payload)
        if not isinstance(raw, dict) or raw.get("schema") != 1:
            raise ValueError("unsupported Redis cache payload schema")
        name = str(raw.get("type") or "")
        response_type = self._types.get(name)
        if response_type is None:
            raise ValueError(f"unregistered Redis cache response type: {name}")
        return response_type.model_validate(raw.get("value"))


class RedisModelCache:
    """Persist typed model responses in Redis with explicit TTL and schema validation."""

    def __init__(
        self,
        connections: RedisConnectionLifecycle,
        *,
        key_prefix: str,
        codec: PydanticResponseCodec | None = None,
        owns_connections: bool = False,
    ) -> None:
        """Bind a Redis lifecycle, key namespace, and safe response codec."""

        self._connections = connections
        self._prefix = key_prefix.rstrip(":")
        self._codec = codec or PydanticResponseCodec()
        self._owns_connections = owns_connections

    def get(self, key: str) -> BaseModel | None:
        """Read and validate one cached response from Redis."""

        payload = self._connections.sync().get(self._key(key))
        return None if payload is None else self._codec.decode(payload)

    def set(self, key: str, value: BaseModel, ttl_seconds: int) -> None:
        """Store one typed response with a Redis expiry."""

        self._connections.sync().set(self._key(key), self._codec.encode(value), ex=ttl_seconds)

    async def aget(self, key: str) -> BaseModel | None:
        """Read and validate one cached response asynchronously."""

        payload = await self._connections.async_client().get(self._key(key))
        return None if payload is None else self._codec.decode(payload)

    async def aset(self, key: str, value: BaseModel, ttl_seconds: int) -> None:
        """Store one typed response asynchronously with a Redis expiry."""

        await self._connections.async_client().set(
            self._key(key), self._codec.encode(value), ex=ttl_seconds
        )

    def close(self) -> None:
        """Close the shared Redis lifecycle only when this cache owns it."""

        if self._owns_connections:
            self._connections.close()

    async def aclose(self) -> None:
        """Close owned Redis resources through the asynchronous boundary."""

        if self._owns_connections:
            await self._connections.aclose()

    def _key(self, key: str) -> str:
        return f"{self._prefix}:cache:{key}"


def default_response_types() -> dict[str, type[BaseModel]]:
    """Return stable Harbor core response types allowed in Redis cache payloads."""

    from harborrag_core.models.chat import HarborChatResponse
    from harborrag_core.models.embed import HarborEmbedResponse
    from harborrag_core.models.rerank import HarborRerankResponse

    return {
        "chat": HarborChatResponse,
        "embed": HarborEmbedResponse,
        "rerank": HarborRerankResponse,
    }
