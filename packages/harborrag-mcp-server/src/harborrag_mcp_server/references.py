"""Short-lived typed references for model-facing knowledge tools."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from threading import RLock


@dataclass(frozen=True, slots=True)
class ReferenceValue:
    kind: str
    value: str
    tenant_id: str
    principal_id: str
    expires_at: float


@dataclass(slots=True)
class KnowledgeReferenceStore:
    """Issue opaque handles and re-bind every read to its authenticated owner."""

    ttl_seconds: int = 24 * 60 * 60
    max_entries: int = 100_000
    _values: dict[str, ReferenceValue] = field(default_factory=dict, init=False, repr=False)
    _reverse: dict[tuple[str, str, str, str], str] = field(
        default_factory=dict, init=False, repr=False
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.ttl_seconds < 1:
            raise ValueError("knowledge reference ttl_seconds must be positive")
        if self.max_entries < 1:
            raise ValueError("knowledge reference max_entries must be positive")

    def issue(
        self,
        kind: str,
        value: str,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> str:
        prefix = {
            "evidence": "ev",
            "entity": "ent",
            "document": "doc",
            "source": "src",
            "relation": "rel",
            "cursor": "cur",
        }.get(kind)
        if prefix is None or not value.strip() or not tenant_id.strip() or not principal_id.strip():
            raise ValueError("knowledge reference identity is invalid")
        key = (kind, value, tenant_id, principal_id)
        now = time.time()
        with self._lock:
            self._discard_expired(now)
            current = self._reverse.get(key)
            if current is not None and current in self._values:
                return current
            while len(self._values) >= self.max_entries:
                oldest = next(iter(self._values))
                self._discard(oldest)
            handle = f"{prefix}_{secrets.token_urlsafe(18)}"
            self._values[handle] = ReferenceValue(
                kind,
                value,
                tenant_id,
                principal_id,
                now + self.ttl_seconds,
            )
            self._reverse[key] = handle
            return handle

    def resolve(
        self,
        handle: str,
        kind: str,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> str | None:
        now = time.time()
        with self._lock:
            entry = self._values.get(handle)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._discard(handle)
                return None
            if (
                entry.kind != kind
                or entry.tenant_id != tenant_id
                or entry.principal_id != principal_id
            ):
                return None
            return entry.value

    def _discard_expired(self, now: float) -> None:
        for handle, entry in tuple(self._values.items()):
            if entry.expires_at <= now:
                self._discard(handle)

    def _discard(self, handle: str) -> None:
        entry = self._values.pop(handle, None)
        if entry is not None:
            self._reverse.pop((entry.kind, entry.value, entry.tenant_id, entry.principal_id), None)


__all__ = ["KnowledgeReferenceStore", "ReferenceValue"]
