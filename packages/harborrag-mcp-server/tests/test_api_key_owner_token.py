"""In api_key mode the static owner token needs an explicitly bound tenant.

The owner token is a static, never-expiring credential with admin scope. Beside
reader keys, which exist for remote access through a TLS proxy, it must not
also read every tenant: it is accepted only when HARBORRAG_MCP_READER_TENANT_ID
names one concrete tenant.
"""

from __future__ import annotations

import argparse
import logging

import httpx
import pytest

from harborrag_mcp_server.__main__ import _http_auth
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.configuration import McpConfiguration, McpConfigurationStore
from harborrag_mcp_server.server import McpServer, create_mcp_server
from harborrag_mcp_server.server.http import ADMIN_SCOPE, register_http_routes

TOKEN = "local-test-token-that-is-at-least-32-bytes"


class _NoKeys:
    async def verify_key(self, token: str):
        return None


@pytest.fixture
def api_key_mode(monkeypatch):
    import harborrag_runtime.composition.mcp_auth as mcp_auth

    monkeypatch.setattr(mcp_auth, "build_api_key_verification", lambda settings: _NoKeys())
    monkeypatch.setenv("HARBORRAG_MCP_AUTH_MODE", "api_key")
    monkeypatch.setenv("HARBORRAG_MCP_KEY_STORE", "postgres")
    monkeypatch.setenv("HARBORRAG_MCP_BEARER_TOKEN", TOKEN)
    monkeypatch.delenv("HARBORRAG_MCP_READER_TENANT_ID", raising=False)


def _auth():
    arguments = argparse.Namespace(host="127.0.0.1", port=8010, path="/mcp")
    return _http_auth(argparse.ArgumentParser(), arguments, None)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant", [None, "*", " * ", "  "])
async def test_owner_token_is_refused_without_a_concrete_tenant(
    api_key_mode, monkeypatch, caplog, tenant
) -> None:
    if tenant is not None:
        monkeypatch.setenv("HARBORRAG_MCP_READER_TENANT_ID", tenant)

    with caplog.at_level(logging.WARNING):
        verifier = _auth()

    assert await verifier.verify_token(TOKEN) is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("HARBORRAG_MCP_READER_TENANT_ID" in message for message in warnings)


@pytest.mark.asyncio
async def test_owner_token_bound_to_a_tenant_keeps_the_status_ui(
    api_key_mode, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HARBORRAG_MCP_READER_TENANT_ID", "demo")
    verifier = _auth()

    access = await verifier.verify_token(TOKEN)
    assert access is not None
    assert access.claims["role"] == "owner"
    assert access.claims["tenants"] == ["demo"]
    assert ADMIN_SCOPE in access.scopes

    audit = McpAuditLog(path=tmp_path / "audit.jsonl")
    registry = McpServer(audit=audit)
    configuration = McpConfigurationStore(
        path=tmp_path / "mcp.yaml",
        configuration=McpConfiguration(),
        specs=registry.list_tools(),
        audit=audit,
        environment={},
    )
    registry.configuration = configuration
    server = create_mcp_server(registry=registry, auth=verifier)
    register_http_routes(
        server,
        mcp_path="/mcp",
        registry=registry,
        configuration=configuration,
        token_verifier=verifier,
    )
    app = server.http_app(path="/mcp")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            config_response = await client.get("/api/config", headers=headers)
            catalog = await client.get("/api/tools", headers=headers)
            cross_tenant = await client.get("/api/tools?tenant_id=other", headers=headers)

    assert config_response.status_code == 200
    assert catalog.status_code == 200
    assert catalog.json()["tenant_id"] == "demo"
    assert cross_tenant.status_code == 403
