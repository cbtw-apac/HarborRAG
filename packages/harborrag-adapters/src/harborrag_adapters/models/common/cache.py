from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Any, Protocol

from pydantic import BaseModel, SecretStr

from .config import CacheBackend, CacheConfig


class ModelResponseCache(Protocol):
    """Define the cache boundary accepted by all model clients."""

    def get(self, key: str) -> BaseModel | None: ...

    def set(self, key: str, value: BaseModel, ttl_seconds: int) -> None: ...

    async def aget(self, key: str) -> BaseModel | None: ...

    async def aset(self, key: str, value: BaseModel, ttl_seconds: int) -> None: ...

    def close(self) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class CacheEntry:
    """Hold a cached response and its monotonic expiration time."""

    expires_at: float
    value: BaseModel


class InMemoryModelCache:
    """Store bounded, isolated response copies with monotonic TTL expiry."""

    def __init__(self, *, max_entries: int = 1_024, clock: Any = time.monotonic) -> None:
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[str, CacheEntry] = {}
        self._lock = RLock()

    def get(self, key: str) -> BaseModel | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                self._entries.pop(key, None)
                return None
            return entry.value.model_copy(deep=True)

    def set(self, key: str, value: BaseModel, ttl_seconds: int) -> None:
        with self._lock:
            self._purge_expired()
            if key not in self._entries and len(self._entries) >= self._max_entries:
                oldest = min(self._entries, key=lambda item: self._entries[item].expires_at)
                self._entries.pop(oldest, None)
            self._entries[key] = CacheEntry(
                expires_at=self._clock() + ttl_seconds,
                value=value.model_copy(deep=True),
            )

    async def aget(self, key: str) -> BaseModel | None:
        return self.get(key)

    async def aset(self, key: str, value: BaseModel, ttl_seconds: int) -> None:
        self.set(key, value, ttl_seconds)

    def close(self) -> None:
        with self._lock:
            self._entries.clear()

    async def aclose(self) -> None:
        self.close()

    def _purge_expired(self) -> None:
        now = self._clock()
        for key in [key for key, entry in self._entries.items() if entry.expires_at <= now]:
            self._entries.pop(key, None)


@dataclass(frozen=True, slots=True)
class CacheDecision:
    """Describe cache eligibility without exposing request or tenant data."""

    key: str | None
    reason: str

    @property
    def allowed(self) -> bool:
        return self.key is not None


class ResponseCacheController:
    """Apply Harbor cache safety policy and normalized cache metadata."""

    def __init__(
        self,
        config: CacheConfig,
        *,
        family: str,
        backend: ModelResponseCache | None = None,
    ) -> None:
        self.config = config
        self.family = family
        self.backend = backend or InMemoryModelCache(max_entries=config.max_entries)

    def decision(self, request: BaseModel, logical_model: str) -> CacheDecision:
        if not self.config.enabled:
            return CacheDecision(None, "disabled")
        if not bool(getattr(request, "cacheable", False)):
            return CacheDecision(None, "request_bypass")
        if bool(getattr(request, "sensitive", False)) and not self.config.cache_sensitive_requests:
            return CacheDecision(None, "sensitive")
        metadata = getattr(request, "metadata", None)
        tenant_id = getattr(metadata, "tenant_id", None)
        if self.config.require_tenant_id and not tenant_id:
            return CacheDecision(None, "missing_tenant")
        key = deterministic_cache_key(
            family=self.family,
            logical_model=logical_model,
            tenant_id=tenant_id or "__unscoped__",
            request=request,
            namespace=self.config.key_namespace,
        )
        return CacheDecision(key, "eligible")

    def get(self, decision: CacheDecision) -> BaseModel | None:
        if self.config.backend is not CacheBackend.CUSTOM:
            return None
        return self.backend.get(decision.key) if decision.key is not None else None

    async def aget(self, decision: CacheDecision) -> BaseModel | None:
        if self.config.backend is not CacheBackend.CUSTOM:
            return None
        return await self.backend.aget(decision.key) if decision.key is not None else None

    def set(self, decision: CacheDecision, response: BaseModel) -> None:
        if decision.key is not None and self.config.backend is CacheBackend.CUSTOM:
            self.backend.set(decision.key, response, self.config.ttl_seconds)

    async def aset(self, decision: CacheDecision, response: BaseModel) -> None:
        if decision.key is not None and self.config.backend is CacheBackend.CUSTOM:
            await self.backend.aset(decision.key, response, self.config.ttl_seconds)

    def mark_hit(self, response: BaseModel, *, request_id: str | None) -> BaseModel:
        metadata = dict(getattr(response, "provider_metadata", {}))
        metadata["cache"] = {"backend": "harbor", "ttl_seconds": self.config.ttl_seconds}
        return response.model_copy(
            update={
                "cache_hit": True,
                "latency_ms": 0.0,
                "request_id": request_id,
                "provider_metadata": metadata,
            }
        )

    def provider_parameters(self, decision: CacheDecision) -> dict[str, Any]:
        """Return explicit LiteLLM cache controls without leaking tenant identity."""

        if self.config.backend is not CacheBackend.LITELLM:
            return {}
        if decision.key is None:
            return {"caching": False}
        return {
            "caching": True,
            "preset_cache_key": decision.key,
            "ttl": self.config.ttl_seconds,
        }


def deterministic_cache_key(
    *,
    family: str,
    logical_model: str,
    tenant_id: str,
    request: BaseModel,
    namespace: str = "harborrag:model",
) -> str:
    """Hash canonical request semantics with an explicit tenant partition."""

    payload = request.model_dump(mode="python", exclude={"metadata": {"request_id", "trace_id"}})
    canonical = _canonicalize(
        {
            "namespace": namespace,
            "family": family,
            "logical_model": logical_model,
            "tenant_id": tenant_id,
            "request": payload,
        }
    )
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return f"{namespace}:{hashlib.sha256(encoded).hexdigest()}"


def _canonicalize(value: Any) -> Any:
    if isinstance(value, SecretStr):
        digest = hashlib.sha256(value.get_secret_value().encode()).hexdigest()
        return {"secret_sha256": digest}
    if isinstance(value, BaseModel):
        return _canonicalize(value.model_dump(mode="python"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return deepcopy(str(value))
