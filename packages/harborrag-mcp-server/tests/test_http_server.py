from __future__ import annotations

import httpx
import pytest

from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.configuration import McpConfiguration, McpConfigurationStore
from harborrag_mcp_server.server import McpServer, create_mcp_server
from harborrag_mcp_server.server.http import (
    create_local_token_verifier,
    register_http_routes,
    validate_local_http_settings,
)

TOKEN = "local-test-token-that-is-at-least-32-bytes"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"host": "0.0.0.0"}, "may bind only"),
        ({"port": 0}, "port must be between"),
        ({"path": "mcp"}, "path must start"),
        ({"path": "/mcp/"}, "must not end"),
        ({"bearer_token": "short"}, "at least 32"),
    ],
)
def test_local_http_settings_fail_closed(overrides, message) -> None:
    settings = {
        "host": "127.0.0.1",
        "port": 8010,
        "path": "/mcp",
        "bearer_token": TOKEN,
    }
    settings.update(overrides)

    with pytest.raises(ValueError, match=message):
        validate_local_http_settings(**settings)


def test_local_http_settings_accept_loopback() -> None:
    assert (
        validate_local_http_settings(
            host="127.0.0.1",
            port=8010,
            path="/mcp",
            bearer_token=f"  {TOKEN}  ",
        )
        == TOKEN
    )


@pytest.mark.asyncio
async def test_local_token_verifier_accepts_only_configured_token() -> None:
    verifier = create_local_token_verifier(TOKEN)

    access = await verifier.verify_token(TOKEN)

    assert access is not None
    assert access.client_id == "harborrag-local"
    assert access.claims["sub"] == "harborrag-local"
    assert await verifier.verify_token("wrong-token") is None


@pytest.mark.asyncio
async def test_http_routes_expose_ui_health_and_authenticated_mcp(tmp_path) -> None:
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
    verifier = create_local_token_verifier(TOKEN)
    server = create_mcp_server(registry=registry, auth=verifier)
    register_http_routes(
        server,
        mcp_path="/mcp",
        registry=registry,
        configuration=configuration,
        token_verifier=verifier,
    )
    app = server.http_app(path="/mcp")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            page = await client.get("/")
            health = await client.get("/healthz")
            unauthenticated = await client.post(
                "/mcp",
                headers=_mcp_headers(),
                json=_initialize_request(),
            )
            authenticated = await client.post(
                "/mcp",
                headers=_mcp_headers(TOKEN),
                json=_initialize_request(),
            )
            config_without_token = await client.get("/api/config")
            config_response = await client.get(
                "/api/config",
                headers=_owner_headers(),
            )
            tools_without_token = await client.get("/api/tools")
            tools_response = await client.get("/api/tools", headers=_owner_headers())
            call_response = await client.post(
                "/api/tools/call",
                headers=_owner_headers(),
                json={
                    "name": "vector_search",
                    "arguments": {"query": "harbor", "tenant_id": "demo"},
                },
            )

    assert page.status_code == 200
    assert "HarborRAG MCP" in page.text
    assert "Tool Playground" in page.text
    assert "/api/tools/call" in page.text
    assert TOKEN not in page.text
    assert page.headers["content-security-policy"].startswith("default-src 'none'")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "service": "harborrag-mcp",
        "transport": "streamable-http",
        "mcp_path": "/mcp",
        "authentication": "bearer",
        "tool_count": 8,
    }
    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.headers["mcp-session-id"]
    assert '"protocolVersion":"2025-06-18"' in authenticated.text
    assert config_without_token.status_code == 401
    assert config_response.status_code == 200
    assert config_response.json()["configuration"]["version"] == 1
    assert TOKEN not in config_response.text
    assert tools_without_token.status_code == 401
    assert tools_response.status_code == 200
    assert [tool["name"] for tool in tools_response.json()["tools"]] == [
        "vector_search",
        "vector_search_advanced",
        "graph_triplet_search",
        "graph_path_search",
        "graph_subgraph_search",
        "graph_neighborhood",
        "chat",
        "agent",
    ]
    assert call_response.status_code == 200
    assert call_response.json() == {
        "name": "vector_search",
        "result": {"ok": False, "error": "vector retrieval backend is not configured"},
    }
    assert audit.entries[-1]["principal_id"] == "harborrag-local"


@pytest.mark.asyncio
async def test_owner_configuration_api_persists_and_audits_updates(tmp_path) -> None:
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
    verifier = create_local_token_verifier(TOKEN)
    server = create_mcp_server(registry=registry, auth=verifier)
    register_http_routes(
        server,
        mcp_path="/mcp",
        registry=registry,
        configuration=configuration,
        token_verifier=verifier,
    )
    app = server.http_app(path="/mcp")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            current = (await client.get("/api/config", headers=_owner_headers())).json()
            updated = current["configuration"]
            updated["tools"] = {
                "vector_search": {
                    "enabled": True,
                    "defaults": {"top_k": 3},
                    "limits": {"top_k": 7},
                }
            }
            saved = await client.put(
                "/api/config",
                headers=_owner_headers(),
                json={
                    "configuration": updated,
                    "expected_revision": current["revision"],
                },
            )
            stale = await client.put(
                "/api/config",
                headers=_owner_headers(),
                json={
                    "configuration": updated,
                    "expected_revision": current["revision"],
                },
            )

    assert saved.status_code == 200
    assert saved.json()["configuration"]["tools"]["vector_search"]["defaults"] == {"top_k": 3}
    assert saved.json()["restart_required"] is True
    assert stale.status_code == 409
    assert configuration.path.is_file()
    assert "top_k: 3" in configuration.path.read_text(encoding="utf-8")
    assert audit.entries[-1]["event"] == "configuration_changed"
    assert audit.entries[-1]["principal_id"] == "harborrag-local"


@pytest.mark.asyncio
async def test_tool_playground_api_applies_effective_tenant_configuration(tmp_path) -> None:
    audit = McpAuditLog(path=tmp_path / "audit.jsonl")
    registry = McpServer(audit=audit)
    configuration = McpConfigurationStore(
        path=tmp_path / "mcp.yaml",
        configuration=McpConfiguration.model_validate(
            {
                "tools": {
                    "vector_search": {
                        "defaults": {"top_k": 3},
                        "limits": {"top_k": 7},
                    }
                },
                "tenants": {
                    "blocked": {
                        "tools": {"vector_search": {"enabled": False}},
                    }
                },
            }
        ),
        specs=registry.list_tools(),
        audit=audit,
        environment={},
    )
    registry.configuration = configuration
    verifier = create_local_token_verifier(TOKEN)
    server = create_mcp_server(registry=registry, auth=verifier)
    register_http_routes(
        server,
        mcp_path="/mcp",
        registry=registry,
        configuration=configuration,
        token_verifier=verifier,
    )
    app = server.http_app(path="/mcp")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            catalog = await client.get(
                "/api/tools?tenant_id=demo",
                headers=_owner_headers(),
            )
            blocked_catalog = await client.get(
                "/api/tools?tenant_id=blocked",
                headers=_owner_headers(),
            )
            over_limit = await client.post(
                "/api/tools/call",
                headers=_owner_headers(),
                json={
                    "name": "vector_search",
                    "arguments": {"query": "harbor", "tenant_id": "demo", "top_k": 8},
                },
            )
            disabled = await client.post(
                "/api/tools/call",
                headers=_owner_headers(),
                json={
                    "name": "vector_search",
                    "arguments": {"query": "harbor", "tenant_id": "blocked"},
                },
            )
            malformed = await client.post(
                "/api/tools/call",
                headers=_owner_headers(),
                json={"name": "vector_search", "arguments": []},
            )

    vector_schema = next(
        tool["input_schema"] for tool in catalog.json()["tools"] if tool["name"] == "vector_search"
    )
    assert vector_schema["properties"]["top_k"]["default"] == 3
    assert vector_schema["properties"]["top_k"]["maximum"] == 7
    assert "vector_search" not in {tool["name"] for tool in blocked_catalog.json()["tools"]}
    assert over_limit.status_code == 422
    assert disabled.status_code == 403
    assert malformed.status_code == 422
    assert [entry["outcome"] for entry in audit.entries if "outcome" in entry] == [
        "error",
        "error",
    ]


def _mcp_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _owner_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _initialize_request() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "harborrag-test", "version": "1"},
        },
    }
