"""Hashed, tenant-bound bearer keys for an internal MCP resource server."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from fastmcp.server.auth import AccessToken, TokenVerifier

if TYPE_CHECKING:
    from harborrag_engine.security import ApiKeyVerificationService
    from harborrag_runtime.config.mcp_auth_settings import McpAuthSettings


def create_composite_verifier(verifiers: Sequence[TokenVerifier]) -> TokenVerifier:
    """Accept a bearer that any one of ``verifiers`` accepts; the first match wins.

    ``api_key`` mode pairs the hashed reader keys with the loopback owner token,
    so the status UI keeps its configuration editor while every other caller
    presents a key. Each verifier applies its own claims; nothing is merged.
    """

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


class HarborApiKeyVerifier(TokenVerifier):
    """Thin FastMCP adapter: every rule lives in ``ApiKeyVerificationService``.

    Each call is a fresh store lookup, so a key the CLI revokes stops working on
    the next request. ``None`` makes FastMCP answer 401. ``AuthStoreUnavailable``
    is deliberately not caught here: an outage must never look like a bad key,
    and the transport decides how to deny it.

    The claims carry the spec's ``tenant_id``/``owner`` plus the ``role``/
    ``sub``/``tenants`` shape the existing authorisation reads; the tenant always
    comes from the stored row, never from the caller.
    """

    def __init__(self, service: ApiKeyVerificationService) -> None:
        super().__init__(required_scopes=["mcp:read"])
        self._service = service

    async def verify_token(self, token: str) -> AccessToken | None:
        key = await self._service.verify_key(token)
        if key is None:
            return None
        return AccessToken(
            token="",  # never echo the presented key back into the request state
            client_id=f"mcp-key:{key.key_id}",
            subject=key.owner,
            scopes=["mcp:read"],
            expires_at=int(key.expires_at.timestamp()),
            claims={
                "tenant_id": key.tenant_id,
                "owner": key.owner,
                "key_id": key.key_id,
                "auth_method": "api_key",
                "sub": key.owner,
                "role": "reader",
                "tenants": [key.tenant_id],
            },
        )


def build_mcp_auth(settings: McpAuthSettings) -> HarborApiKeyVerifier:
    """Wire the verifier to its own key-store engine: reader -> service -> verifier."""

    from harborrag_runtime.composition.mcp_auth import build_api_key_verification

    return HarborApiKeyVerifier(build_api_key_verification(settings))
