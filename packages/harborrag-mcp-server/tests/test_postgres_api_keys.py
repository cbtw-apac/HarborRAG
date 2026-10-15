"""The database-backed key verifier hands FastMCP the claims this server authorises on."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harborrag_core.ports.api_keys import AuthStoreUnavailable
from harborrag_engine.security import VerifiedKey
from harborrag_mcp_server.server.api_keys import create_postgres_api_key_verifier


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

    token = await create_postgres_api_key_verifier(service).verify_token("hrk_dev_v1_x.y")  # type: ignore[arg-type]

    assert token is not None
    assert token.claims["role"] == "reader"
    assert token.claims["tenants"] == ["engineering"]
    assert token.claims["sub"] == "user-huy"
    assert token.claims["key_id"] == "0" * 24
    assert token.claims["auth_method"] == "api_key"
    assert token.scopes == ["mcp:read"]
    assert token.client_id == "mcp-key:" + "0" * 24
    assert token.expires_at == int(expires.timestamp())
    assert token.token == ""  # the presented key is never stored on the request


@pytest.mark.asyncio
async def test_a_rejected_key_and_a_store_outage_both_deny(caplog) -> None:
    assert await create_postgres_api_key_verifier(FakeService(None)).verify_token("x") is None  # type: ignore[arg-type]

    with caplog.at_level("WARNING", logger="harborrag.mcp.server.api_keys"):
        denied = await create_postgres_api_key_verifier(FakeService(fail=True)).verify_token("x")  # type: ignore[arg-type]

    assert denied is None
    assert "MCP key store lookup failed" in caplog.text


@pytest.mark.asyncio
async def test_the_wired_verifier_denies_when_the_key_table_is_missing(
    tmp_path, monkeypatch
) -> None:
    """End to end through the runtime factory, without migrations: fail closed."""

    from harborrag_runtime.composition.mcp_auth import build_api_key_verification
    from harborrag_runtime.config.settings import RuntimeSettings

    settings = RuntimeSettings(control_db_url=f"sqlite+aiosqlite:///{tmp_path}/empty.db")
    verifier = create_postgres_api_key_verifier(build_api_key_verification(settings))

    raw = "hrk_dev_v1_" + "0" * 24 + "." + "a" * 43
    assert await verifier.verify_token(raw) is None


def _expires_soon() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


@pytest.mark.asyncio
async def test_any_unexpected_verifier_failure_denies_instead_of_erroring(caplog) -> None:
    class Broken:
        async def verify_key(self, raw_key: str):
            raise RuntimeError("postgresql://user:hunter2@db/x")

    with caplog.at_level("ERROR", logger="harborrag.mcp.server.api_keys"):
        denied = await create_postgres_api_key_verifier(Broken()).verify_token("x")  # type: ignore[arg-type]

    assert denied is None
    assert "RuntimeError" in caplog.text and "hunter2" not in caplog.text


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
