"""Hashed, tenant-bound bearer keys for an internal MCP resource server."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from harborrag_core.ports.api_keys import AuthStoreUnavailable

if TYPE_CHECKING:
    from fastmcp.server.auth import TokenVerifier

    from harborrag_engine.security import ApiKeyVerificationService

logger = logging.getLogger("harborrag.mcp.server.api_keys")


class ReaderKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key_id: str = Field(min_length=1, max_length=128)
    principal_id: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(min_length=1, max_length=128)
    secret_hash_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    expires_at: datetime | None = None
    revoked: bool = False

    @field_validator("tenant_id")
    @classmethod
    def tenant_must_be_concrete(cls, value: str) -> str:
        # Grants are stripped when read, so " * " would widen to every tenant too.
        if value != value.strip():
            raise ValueError("MCP key tenant_id must not contain surrounding whitespace")
        if value == "*":
            raise ValueError("MCP key tenant_id must name one tenant, not the '*' wildcard")
        return value


class KeyFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    keys: tuple[ReaderKey, ...] = Field(min_length=1, max_length=1000)


def _load_keys(path: Path) -> KeyFile:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    key_file = KeyFile.model_validate(data)
    if len({key.key_id for key in key_file.keys}) != len(key_file.keys):
        raise ValueError("MCP key IDs must be unique")
    for key in key_file.keys:
        value = os.environ.get(key.secret_hash_env, "")
        if len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value):
            raise ValueError(f"MCP key hash environment variable {key.secret_hash_env} is invalid")
        if key.expires_at is not None and key.expires_at.tzinfo is None:
            raise ValueError("MCP key expiration must include a timezone")
    return key_file


def create_api_key_verifier(path: str | Path) -> TokenVerifier:
    """Verify each request against current revocation/expiry state on disk."""
    from fastmcp.server.auth import AccessToken, TokenVerifier

    key_path = Path(path)
    _load_keys(key_path)  # Fail startup on bad configuration.

    class ApiKeyVerifier(TokenVerifier):
        async def verify_token(self, token: str) -> AccessToken | None:
            if len(token) < 32 or len(token) > 4096:
                return None
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            try:
                records = _load_keys(key_path).keys
            except (OSError, ValueError):
                return None
            for key in records:
                expected = os.environ[key.secret_hash_env]
                if not hmac.compare_digest(digest, expected.lower()):
                    continue
                if key.revoked or (key.expires_at and key.expires_at <= datetime.now(UTC)):
                    return None
                return AccessToken(
                    token="",
                    client_id=key.key_id,
                    subject=key.principal_id,
                    scopes=["mcp:read"],
                    claims={
                        "sub": key.principal_id,
                        "role": "reader",
                        "tenants": [key.tenant_id],
                    },
                )
            return None

    return ApiKeyVerifier(required_scopes=["mcp:read"])


def create_postgres_api_key_verifier(service: ApiKeyVerificationService) -> TokenVerifier:
    """Verify CLI-issued keys against the control-plane table on every request.

    The claims keep the shape the rest of this server already authorises on:
    ``role`` and a one-element ``tenants`` grant taken from the stored row, so a
    caller can never widen its own tenant. An unreachable store denies the
    request; the outage is logged by type so it is not mistaken for a bad key.
    """

    from fastmcp.server.auth import AccessToken, TokenVerifier

    class PostgresApiKeyVerifier(TokenVerifier):
        async def verify_token(self, token: str) -> AccessToken | None:
            try:
                key = await service.verify_key(token)
            except AuthStoreUnavailable as exc:
                logger.warning(
                    "MCP key store lookup failed: %s", type(exc.__cause__ or exc).__name__
                )
                return None
            except Exception as exc:  # noqa: BLE001 - the auth boundary never leaks a 500
                logger.error("MCP key verification failed: %s", type(exc).__name__)
                return None
            if key is None:
                return None
            return AccessToken(
                token="",  # never echo the presented key back into the request state
                client_id=f"mcp-key:{key.key_id}",
                subject=key.owner,
                scopes=["mcp:read"],
                expires_at=int(key.expires_at.timestamp()),
                claims={
                    "sub": key.owner,
                    "role": "reader",
                    "tenants": [key.tenant_id],
                    "key_id": key.key_id,
                    "auth_method": "api_key",
                },
            )

    return PostgresApiKeyVerifier(required_scopes=["mcp:read"])


def create_composite_verifier(verifiers: Sequence[TokenVerifier]) -> TokenVerifier:
    """Accept a bearer that any one of ``verifiers`` accepts; the first match wins.

    ``api_key`` mode pairs the hashed reader keys with the loopback owner token,
    so the status UI keeps its configuration editor while every other caller
    presents a key. Each verifier applies its own claims; nothing is merged.
    """

    from fastmcp.server.auth import AccessToken, TokenVerifier

    if not verifiers:
        raise ValueError("composite verifier needs at least one verifier")

    class CompositeVerifier(TokenVerifier):
        async def verify_token(self, token: str) -> AccessToken | None:
            for verifier in verifiers:
                access = await verifier.verify_token(token)
                if access is not None:
                    return access
            return None

    return CompositeVerifier(required_scopes=["mcp:read"])
