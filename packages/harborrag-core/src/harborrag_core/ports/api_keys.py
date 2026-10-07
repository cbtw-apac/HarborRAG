from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from harborrag_core.security.api_keys import Environment

AuditAction = Literal["mcp_key.created", "mcp_key.revoked"]


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    key_id: str
    tenant_id: str
    owner: str
    name: str
    secret_hash: str = field(repr=False)
    environment: Environment
    created_at: datetime
    created_by: str
    expires_at: datetime
    revoked_at: datetime | None
    revoked_by: str | None
    revocation_reason: str | None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    actor: str
    tenant_id: str
    action: AuditAction  # mcp_key.created or mcp_key.revoked
    entity_id: str  # the key_id
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Revocation:
    at: datetime
    by: str
    reason: str | None = None


class ApiKeyReader(Protocol):
    """used by the MCP server. Read-only"""

    async def get_by_key_id(self, key_id: str) -> ApiKeyRecord | None: ...


class ApiKeyManager(Protocol):
    """Used by the CLI only. Each method is ONE transaction including its audit event."""

    async def create(self, record: ApiKeyRecord, audit: AuditEvent) -> None: ...
    async def revoke(
        self, key_id: str, *, tenant_id: str, revocation: Revocation, audit: AuditEvent
    ) -> bool:
        """False when the key is unknown, belongs to another tenant, or is already revoked."""

    async def revoke_by_owner(
        self, tenant_id: str, owner: str, *, revocation: Revocation
    ) -> list[str]:
        """Revoke every live key of ``owner``; one ``mcp_key.revoked`` audit per key, actor=revocation.by."""

    async def list_for_tenant(self, tenant_id: str) -> list[ApiKeyRecord]:
        """All keys of ``tenant_id``, including revoked and expired ones."""
