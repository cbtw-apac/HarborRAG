"""Hashed, tenant-bound bearer keys for an internal MCP resource server."""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from fastmcp.server.auth import TokenVerifier


class ReaderKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key_id: str = Field(min_length=1, max_length=128)
    principal_id: str = Field(min_length=1, max_length=255)
    tenant_id: str = Field(min_length=1, max_length=128)
    secret_hash_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    expires_at: datetime | None = None
    revoked: bool = False


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
