"""The key verifier hands FastMCP the claims this server authorises on."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harborrag_core.ports.api_keys import AuthStoreUnavailable
from harborrag_engine.security import VerifiedKey
from harborrag_mcp_server.server.api_keys import HarborApiKeyVerifier


class FakeService:
    def __init__(self, result: VerifiedKey | None = None, *, fail: bool = False) -> None:
        self.result = result
        self.fail = fail
        self.presented: list[str] = []

    async def verify_key(self, raw_key: str) -> VerifiedKey | None:
        self.presented.append(raw_key)
        if self.fail:
            raise AuthStoreUnavailable("down")
        return self.result


@pytest.mark.asyncio
async def test_a_verified_key_becomes_a_reader_token_bound_to_its_tenant() -> None:
    expires = datetime(2026, 12, 1, tzinfo=UTC)
    service = FakeService(
        VerifiedKey(key_id="0" * 24, tenant_id="engineering", owner="user-huy", expires_at=expires)
    )

    token = await HarborApiKeyVerifier(service).verify_token("hrk_dev_v1_x.y")  # type: ignore[arg-type]

    assert token is not None
    assert token.claims["role"] == "reader"
    assert token.claims["tenants"] == ["engineering"]
    assert token.claims["sub"] == "user-huy"
    assert token.claims["tenant_id"] == "engineering"
    assert token.claims["owner"] == "user-huy"
    assert token.claims["key_id"] == "0" * 24
    assert token.claims["auth_method"] == "api_key"
    assert token.scopes == ["mcp:read"]
    assert token.client_id == "mcp-key:" + "0" * 24
    assert token.expires_at == int(expires.timestamp())
    assert token.token == ""  # the presented key is never stored on the request


@pytest.mark.asyncio
async def test_a_rejected_key_denies_and_a_store_outage_is_not_swallowed() -> None:
    assert await HarborApiKeyVerifier(FakeService(None)).verify_token("x") is None  # type: ignore[arg-type]

    # An outage must stay distinguishable from a bad key: the transport decides the status.
    with pytest.raises(AuthStoreUnavailable):
        await HarborApiKeyVerifier(FakeService(fail=True)).verify_token("x")  # type: ignore[arg-type]


def test_auth_settings_fail_fast_on_missing_or_empty_configuration(tmp_path, monkeypatch) -> None:
    from pydantic import ValidationError

    from harborrag_runtime.config.mcp_auth_settings import McpAuthSettings

    for name in ("AUTH_MODE", "KEY_STORE", "ENVIRONMENT", "AUTH_DATABASE_DSN_FILE"):
        monkeypatch.delenv(f"HARBORRAG_MCP_{name}", raising=False)
    with pytest.raises(ValidationError):
        McpAuthSettings()

    dsn = tmp_path / "dsn"
    dsn.write_text("   ", encoding="utf-8")
    monkeypatch.setenv("HARBORRAG_MCP_AUTH_MODE", "api_key")
    monkeypatch.setenv("HARBORRAG_MCP_KEY_STORE", "postgres")
    monkeypatch.setenv("HARBORRAG_MCP_ENVIRONMENT", "prod")
    monkeypatch.setenv("HARBORRAG_MCP_AUTH_DATABASE_DSN_FILE", str(dsn))
    settings = McpAuthSettings()
    with pytest.raises(ValueError, match="empty"):
        settings.read_database_dsn()


@pytest.mark.asyncio
async def test_the_wired_verifier_fails_closed_when_the_key_table_is_missing(tmp_path) -> None:
    """End to end through the runtime factory, without migrations."""

    from harborrag_mcp_server.server.api_keys import build_mcp_auth
    from harborrag_runtime.config.mcp_auth_settings import McpAuthSettings

    dsn = tmp_path / "dsn"
    dsn.write_text(f"sqlite+aiosqlite:///{tmp_path}/empty.db", encoding="utf-8")
    settings = McpAuthSettings(
        auth_mode="api_key",
        key_store="postgres",
        environment="dev",
        auth_database_dsn_file=dsn,
    )
    verifier = build_mcp_auth(settings)

    raw = "hrk_dev_v1_" + "0" * 24 + "." + "a" * 43
    with pytest.raises(AuthStoreUnavailable):
        await verifier.verify_token(raw)


def _expires_soon() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


@pytest.mark.asyncio
async def test_composite_verifier_accepts_whichever_verifier_knows_the_token() -> None:
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    from harborrag_mcp_server.server.api_keys import create_composite_verifier

    owner = StaticTokenVerifier(
        tokens={"owner-token": {"client_id": "o", "role": "owner", "scopes": ["mcp:read"]}}
    )
    reader = StaticTokenVerifier(
        tokens={"reader-token": {"client_id": "r", "role": "reader", "scopes": ["mcp:read"]}}
    )
    composite = create_composite_verifier([owner, reader])

    assert (await composite.verify_token("owner-token")).claims["role"] == "owner"
    assert (await composite.verify_token("reader-token")).claims["role"] == "reader"
    assert await composite.verify_token("nope") is None
    with pytest.raises(ValueError):
        create_composite_verifier([])
