"""Storage contracts for MCP reader keys.

``ApiKeyReader`` is all the MCP server ever receives: one indexed lookup per
request, no caching, so a key created or revoked by the CLI takes effect on the
next call. ``ApiKeyManager`` is the CLI's write side; each method is one
transaction that also records its own audit entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from harborrag_core.schemas.storage import StorageOperationContext


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    key_id: str
    tenant_id: str
    owner: str
    name: str
    secret_hash: str
    environment: str
    created_at: datetime
    created_by: str
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    revocation_reason: str | None = None


class AuthStoreUnavailable(Exception):
    """The key store could not be reached; callers must deny access."""


class ApiKeyReader(Protocol):
    async def get_by_key_id(
        self, key_id: str, *, context: StorageOperationContext
    ) -> ApiKeyRecord | None:
        """Look a key up by id alone; the tenant is not known yet, so this needs a platform context."""
        ...


class ApiKeyManager(Protocol):
    """Tenant-bound operations take a context scoped to that tenant; operations that
    are global by nature (a key id names its own tenant) take a platform context."""

    async def create(self, record: ApiKeyRecord, *, context: StorageOperationContext) -> None: ...

    async def get_by_key_id(
        self, key_id: str, *, context: StorageOperationContext
    ) -> ApiKeyRecord | None: ...

    async def revoke(
        self, key_id: str, *, at: datetime, by: str, reason: str, context: StorageOperationContext
    ) -> bool:
        """Mark one active key revoked; False when unknown or already revoked."""
        ...

    async def revoke_by_owner(  # noqa: PLR0913 - the tenant, owner, audit fields and the storage context
        self,
        tenant_id: str,
        owner: str,
        *,
        at: datetime,
        by: str,
        reason: str,
        context: StorageOperationContext,
    ) -> list[str]:
        """Revoke every active key of ``owner`` in ``tenant_id``; returns their ids."""
        ...

    async def list_for_tenant(
        self, tenant_id: str, *, context: StorageOperationContext
    ) -> list[ApiKeyRecord]: ...


__all__ = ["ApiKeyManager", "ApiKeyReader", "ApiKeyRecord", "AuthStoreUnavailable"]
