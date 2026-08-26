from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from harborrag_adapters.repositories.policies.key_encoding import escape_key_part
from harborrag_adapters.repositories.telemetry import RepositoryTelemetry
from harborrag_core.schemas.cache import CacheEntry, LockHandle
from harborrag_core.storage import StorageOperationContext


@dataclass(slots=True)
class MemoryCacheState:
    """Process-local cache data owned by exactly one backend instance."""

    instance_name: str
    key_prefix: str
    telemetry: RepositoryTelemetry
    entries: dict[str, CacheEntry] = field(default_factory=dict)
    tags: dict[str, set[str]] = field(default_factory=dict)
    locks: dict[str, LockHandle] = field(default_factory=dict)
    fencing_tokens: dict[str, int] = field(default_factory=dict)
    mutex: asyncio.Lock = field(default_factory=asyncio.Lock)
    connected: bool = False

    def key(self, context: StorageOperationContext, value: str) -> str:
        return f"{self.key_prefix}:{context.tenant_id}:{escape_key_part(value)}"

    @staticmethod
    def tag(context: StorageOperationContext, value: str) -> str:
        return f"{context.tenant_id}:{escape_key_part(value)}"
