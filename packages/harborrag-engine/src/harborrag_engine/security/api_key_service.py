"""Rules for MCP reader keys: what makes a presented key valid, and how one is issued.

No SQL and no transport here. Storage comes in through the ``harborrag_core``
ports, so both services are unit-tested with in-memory fakes.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from harborrag_core.ports.api_keys import ApiKeyManager, ApiKeyReader, ApiKeyRecord
from harborrag_core.ports.tenants import TenantDirectory
from harborrag_core.schemas.ids import TenantId
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.security.api_keys import generate_key, hash_matches, parse_key
from harborrag_core.security.context import AccessContext

MIN_LIFETIME = timedelta(hours=1)
MAX_LIFETIME: dict[str, timedelta] = {
    "dev": timedelta(days=7),
    "staging": timedelta(days=90),
    "prod": timedelta(days=90),
}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_OWNER_RE = re.compile(r"^(user|svc)-[a-z0-9][a-z0-9-]{0,120}$")
_MAX_REASON = 256
_VERIFIER = "harborrag-mcp-server"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ApiKeyPolicyError(ValueError):
    """A create or revoke request that the key policy rejects."""


class TenantNotFound(ApiKeyPolicyError):
    """A key was requested for a tenant that no ingestion has been submitted for."""


class KeyNotFound(ApiKeyPolicyError):
    """A revoke named a key id that does not exist."""


@dataclass(frozen=True, slots=True)
class VerifiedKey:
    key_id: str
    tenant_id: str
    owner: str
    expires_at: datetime


class ApiKeyVerificationService:
    """Decide whether a presented key may read one tenant."""

    def __init__(
        self,
        reader: ApiKeyReader,
        *,
        environment: str,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._reader = reader
        self._environment = environment
        self._clock = clock

    async def verify_key(self, raw_key: str) -> VerifiedKey | None:
        """None means deny. ``AuthStoreUnavailable`` propagates: deny, but distinguishable."""

        parsed = parse_key(raw_key)
        if parsed is None or parsed.environment != self._environment:
            return None  # malformed or from another environment: no lookup at all
        # The key id is all a request carries: its tenant is read from the record,
        # so the lookup runs under the explicit platform scope.
        context = StorageOperationContext.platform(_VERIFIER, operation_kind="mcp_key.verify")
        record = await self._reader.get_by_key_id(parsed.key_id, context=context)
        if record is None:
            return None
        if not hash_matches(raw_key, record.secret_hash):
            return None
        if record.revoked_at is not None:
            return None
        if record.expires_at <= self._clock():
            return None
        if record.environment != self._environment:
            return None
        if record.tenant_id == "*":
            # A stored wildcard would become an every-tenant grant downstream;
            # issuance refuses it, and so does verification, in case a row was
            # written by other means.
            return None
        return VerifiedKey(
            key_id=record.key_id,
            tenant_id=record.tenant_id,
            owner=record.owner,
            expires_at=record.expires_at,
        )


@dataclass(frozen=True, slots=True)
class CreatedKey:
    key_id: str
    tenant_id: str
    owner: str
    name: str
    expires_at: datetime
    raw_key: str = field(repr=False)  # delivered once by the CLI, never logged


class ApiKeyManagementService:
    """Issue and revoke keys under the lifetime and naming policy."""

    def __init__(
        self,
        manager: ApiKeyManager,
        tenants: TenantDirectory,
        *,
        environment: str,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if environment not in MAX_LIFETIME:
            raise ValueError(f"unsupported key environment: {environment!r}")
        self._manager = manager
        self._tenants = tenants
        self._environment = environment
        self._clock = clock

    @property
    def max_lifetime(self) -> timedelta:
        return MAX_LIFETIME[self._environment]

    async def create_key(
        self, *, operator: str, tenant_id: str, owner: str, name: str, lifetime: timedelta
    ) -> CreatedKey:
        self._validate(tenant_id=tenant_id, owner=owner, name=name, lifetime=lifetime)
        discovery = StorageOperationContext.platform(operator, operation_kind="tenant.discover")
        if not await self._tenants.exists(tenant_id, context=discovery):
            # Names no value: the CLI echoes this message to the operator.
            raise TenantNotFound(
                "tenant_id names no known tenant; submit an ingestion for it first"
            )
        now = self._clock()
        new = generate_key(self._environment)
        record = ApiKeyRecord(
            key_id=new.key_id,
            tenant_id=tenant_id,
            owner=owner,
            name=name,
            secret_hash=new.secret_hash,
            environment=self._environment,
            created_at=now,
            created_by=operator,
            expires_at=now + lifetime,
        )
        await self._manager.create(
            record, context=self._tenant_context(operator, tenant_id, "mcp_key.create")
        )
        return CreatedKey(
            key_id=new.key_id,
            tenant_id=tenant_id,
            owner=owner,
            name=name,
            expires_at=record.expires_at,
            raw_key=new.raw_key,
        )

    async def revoke_key(self, *, operator: str, key_id: str, reason: str) -> bool:
        self._validate_reason(reason)
        # A key id names its own tenant, so revoking by id is a platform operation.
        context = StorageOperationContext.platform(operator, operation_kind="mcp_key.revoke")
        revoked = await self._manager.revoke(
            key_id, at=self._clock(), by=operator, reason=reason, context=context
        )
        # False means unknown or already revoked; keys are never deleted, so a
        # lookup after the fact tells the two apart without a race.
        if not revoked and await self._manager.get_by_key_id(key_id, context=context) is None:
            raise KeyNotFound("no key with this id")  # no input echoed
        return revoked

    async def revoke_owner(
        self, *, operator: str, tenant_id: str, owner: str, reason: str
    ) -> list[str]:
        self._validate_reason(reason)
        context = self._tenant_context(operator, tenant_id, "mcp_key.revoke_owner")
        return await self._manager.revoke_by_owner(
            tenant_id, owner, at=self._clock(), by=operator, reason=reason, context=context
        )

    async def list_keys(self, *, operator: str, tenant_id: str) -> list[ApiKeyRecord]:
        context = self._tenant_context(operator, tenant_id, "mcp_key.list")
        return await self._manager.list_for_tenant(tenant_id, context=context)

    @staticmethod
    def _tenant_context(
        operator: str, tenant_id: str, operation_kind: str
    ) -> StorageOperationContext:
        if not tenant_id.strip():
            raise ApiKeyPolicyError("tenant_id must not be empty")
        if tenant_id != tenant_id.strip():
            raise ApiKeyPolicyError("tenant_id must not have surrounding whitespace")
        access = AccessContext(principal_id=operator, tenant_id=TenantId(tenant_id))
        return StorageOperationContext.for_access(access, operation_kind=operation_kind)

    def _validate(self, *, tenant_id: str, owner: str, name: str, lifetime: timedelta) -> None:
        if (
            not tenant_id
            or tenant_id != tenant_id.strip()
            or any(ch.isspace() for ch in tenant_id)
            or len(tenant_id) > 128
        ):
            raise ApiKeyPolicyError("tenant_id must be a non-empty identifier without whitespace")
        if tenant_id == "*":
            # The transport reads "*" as "every tenant"; a reader key names one.
            raise ApiKeyPolicyError("tenant_id must name one tenant, not the '*' wildcard")
        if not _OWNER_RE.fullmatch(owner):
            raise ApiKeyPolicyError(
                "owner must look like user-<name>, svc-<name> (lowercase, digits, '-')"
            )
        if not _NAME_RE.fullmatch(name):
            raise ApiKeyPolicyError("name must be 1-128 lowercase letters, digits or '-'")
        if lifetime < MIN_LIFETIME:
            raise ApiKeyPolicyError("lifetime must be at least 1 hour")
        if lifetime > self.max_lifetime:
            days = self.max_lifetime.days
            raise ApiKeyPolicyError(f"lifetime must not exceed {days} days in {self._environment}")

    @staticmethod
    def _validate_reason(reason: str) -> None:
        if not reason.strip() or len(reason) > _MAX_REASON:
            raise ApiKeyPolicyError("a revocation reason of at most 256 characters is required")


__all__ = [
    "MAX_LIFETIME",
    "MIN_LIFETIME",
    "ApiKeyManagementService",
    "ApiKeyPolicyError",
    "ApiKeyVerificationService",
    "CreatedKey",
    "KeyNotFound",
    "TenantNotFound",
    "VerifiedKey",
]
