from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import httpx
import pytest
from catalog_support import EXPECTED_READER_TOOLS

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.schemas.ids import TenantId
from harborrag_engine.tools.base import BaseTool, ToolSpec
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.configuration import McpConfiguration, McpConfigurationStore
from harborrag_mcp_server.server import McpServer, create_mcp_server
from harborrag_mcp_server.server import http as http_module
from harborrag_mcp_server.server.http import (
    RouteAccess,
    create_local_token_verifier,
    register_http_routes,
    validate_local_http_settings,
)

TOKEN = "local-test-token-that-is-at-least-32-bytes"


class _TenantDirectory:
    """TenantDirectory stub that lists fixed tenants, or fails with ``failure``."""

    def __init__(self, tenants: list[str], failure: Exception | None = None) -> None:
        self.tenants = tenants
        self.failure = failure
        self.lookups = 0

    async def tenant_ids(self) -> list[TenantId]:
        self.lookups += 1
        if self.failure is not None:
            raise self.failure
        return [TenantId(tenant) for tenant in self.tenants]


_STATUS_PAGE = Path(http_module.__file__).parent / "static" / "status.html"


@pytest.mark.unit
def test_status_page_markup_has_every_element_its_script_selects() -> None:
    markup, _, script = _STATUS_PAGE.read_text(encoding="utf-8").partition("<script")

    selected = set(re.findall(r"querySelector\('#([\w-]+)'\)", script))

    assert len(selected) >= 10
    assert [name for name in sorted(selected) if f'id="{name}"' not in markup] == []


@pytest.mark.unit
@pytest.mark.parametrize("placeholder", ["__MCP_ENDPOINT__", "__MCP_TOOLS__", "__MCP_NONCE__"])
def test_status_page_keeps_each_server_filled_placeholder(placeholder: str) -> None:
    assert _STATUS_PAGE.read_text(encoding="utf-8").count(placeholder) == 1


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
    assert access.claims["tenants"] == ["*"]
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
        access=RouteAccess(verifier),
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
            oversized_call = await client.post(
                "/api/tools/call",
                headers=_owner_headers(),
                content=b" " * (128 * 1024 + 1),
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
        "tool_count": 11,
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
    assert [tool["name"] for tool in tools_response.json()["tools"]] == EXPECTED_READER_TOOLS
    assert call_response.status_code == 200
    assert call_response.json()["name"] == "vector_search"
    assert call_response.json()["result"] == {
        "ok": False,
        "error": "vector retrieval backend is not configured",
    }
    assert audit.entries[-1]["principal_id"] == "harborrag-local"
    assert oversized_call.status_code == 422
    assert oversized_call.json()["error"] == "request body budget exceeded"


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
        access=RouteAccess(verifier),
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


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.geteuid() == 0, reason="root can write into a directory without write permission"
)
async def test_configuration_api_answers_409_when_the_file_is_read_only(tmp_path) -> None:
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    audit = McpAuditLog(path=tmp_path / "audit.jsonl")
    registry = McpServer(audit=audit)
    configuration = McpConfigurationStore(
        path=mounted / "mcp.yaml",
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
        access=RouteAccess(verifier),
    )
    app = server.http_app(path="/mcp")
    mounted.chmod(0o555)

    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                current = (await client.get("/api/config", headers=_owner_headers())).json()
                saved = await client.put(
                    "/api/config",
                    headers=_owner_headers(),
                    json={
                        "configuration": current["configuration"],
                        "expected_revision": current["revision"],
                    },
                )
    finally:
        mounted.chmod(0o755)

    assert current["writable"] is False
    assert saved.status_code == 409
    assert "read-only" in saved.json()["error"]
    assert audit.entries == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_playground_offers_the_tenants_the_server_was_started_with(tmp_path) -> None:
    audit = McpAuditLog(path=tmp_path / "audit.jsonl")
    registry = McpServer(
        audit=audit,
        known_tenants=("DEFAULT",),
        corpus_mode="tenant_shared",
        shared_tenant_id="shared",
        tenant_directory=_TenantDirectory(["DEFAULT", "tenant-1"]),
    )
    configuration = McpConfigurationStore(
        path=tmp_path / "mcp.yaml",
        configuration=McpConfiguration.model_validate(
            {"tenants": {"override": {"tools": {"vector_search": {"enabled": False}}}}}
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
        access=RouteAccess(verifier),
    )
    app = server.http_app(path="/mcp")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            catalog = await client.get("/api/tools", headers=_owner_headers())
            lookups_on_connect = registry.tenant_directory.lookups
            for_one_tenant = await client.get(
                "/api/tools?tenant_id=DEFAULT", headers=_owner_headers()
            )

    assert catalog.json()["tenants"] == ["DEFAULT", "override", "shared", "tenant-1"]
    assert catalog.json()["any_tenant"] is True
    assert "tenants" not in for_one_tenant.json()
    assert registry.tenant_directory.lookups == lookups_on_connect == 1


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [HarborUnavailableError("database is down"), TimeoutError()])
async def test_tenant_choices_fall_back_to_the_configured_tenants(
    tmp_path, failure: Exception
) -> None:
    registry = McpServer(
        audit=McpAuditLog(path=tmp_path / "audit.jsonl"),
        known_tenants=("DEFAULT",),
        tenant_directory=_TenantDirectory([], failure=failure),
    )

    assert await registry.tenant_choices() == ["DEFAULT"]


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
        access=RouteAccess(verifier),
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

    search_schema = next(
        tool["input_schema"] for tool in catalog.json()["tools"] if tool["name"] == "vector_search"
    )
    assert search_schema["properties"]["top_k"]["default"] == 3
    assert search_schema["properties"]["top_k"]["maximum"] == 7
    assert "vector_search" not in {tool["name"] for tool in blocked_catalog.json()["tools"]}
    assert over_limit.status_code == 422
    assert disabled.status_code == 403
    assert malformed.status_code == 422
    assert [entry["outcome"] for entry in audit.entries if "outcome" in entry] == [
        "error",
        "error",
    ]


@pytest.mark.asyncio
async def test_a_tenant_bound_owner_token_still_administers_and_binds_its_tenant(tmp_path) -> None:
    """HARBORRAG_MCP_READER_TENANT_ID scopes reads; it must not revoke ownership.

    Binding the local token to a shared corpus made every owner route answer
    403 "token is not authorized for the requested tenant" -- the configuration
    API, the tool catalog and the playground -- with the correct bearer token.
    """

    audit = McpAuditLog(path=tmp_path / "audit.jsonl")
    other_tenants = _TenantDirectory(["finance"])
    registry = McpServer(
        audit=audit,
        corpus_mode="tenant_shared",
        shared_tenant_id="demo",
        tenant_directory=other_tenants,
    )
    configuration = McpConfigurationStore(
        path=tmp_path / "mcp.yaml",
        configuration=McpConfiguration(),
        specs=registry.list_tools(),
        audit=audit,
        environment={},
    )
    registry.configuration = configuration
    verifier = create_local_token_verifier(TOKEN, tenant_id="demo")
    server = create_mcp_server(registry=registry, auth=verifier)
    register_http_routes(
        server,
        mcp_path="/mcp",
        registry=registry,
        configuration=configuration,
        access=RouteAccess(verifier),
    )
    app = server.http_app(path="/mcp")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            config_response = await client.get("/api/config", headers=_owner_headers())
            reload_response = await client.post("/api/config/reload", headers=_owner_headers())
            catalog = await client.get("/api/tools", headers=_owner_headers())
            call_response = await client.post(
                "/api/tools/call",
                headers=_owner_headers(),
                json={"name": "vector_search", "arguments": {"query": "harbor"}},
            )
            cross_tenant = await client.post(
                "/api/tools/call",
                headers=_owner_headers(),
                json={
                    "name": "vector_search",
                    "arguments": {"query": "harbor", "tenant_id": "other"},
                },
            )

    assert config_response.status_code == 200
    # Reaching the store at all is the point: with no file written yet the reload
    # reports 422, but authorization no longer turns it into a 403.
    assert reload_response.status_code != 403
    assert catalog.status_code == 200
    assert catalog.json()["tenant_id"] == "demo"
    assert catalog.json()["tenants"] == ["demo"]
    assert catalog.json()["any_tenant"] is False
    assert other_tenants.lookups == 0
    assert [tool["name"] for tool in catalog.json()["tools"]] == EXPECTED_READER_TOOLS
    assert call_response.status_code == 200
    # The call was bound to the token's tenant instead of falling through to the
    # "local" tenant, where source ACLs would have returned an empty corpus.
    assert [entry["tenant_id"] for entry in audit.entries][-1] == "demo"
    assert cross_tenant.status_code == 403
    assert cross_tenant.json()["error"] == "token is not authorized for the requested tenant"


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


@pytest.mark.asyncio
async def test_reader_keys_use_the_playground_but_not_the_configuration(tmp_path) -> None:
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    audit = McpAuditLog(path=tmp_path / "audit.jsonl")
    other_tenants = _TenantDirectory(["finance"])
    registry = McpServer(audit=audit, tenant_directory=other_tenants)
    configuration = McpConfigurationStore(
        path=tmp_path / "mcp.yaml",
        configuration=McpConfiguration(),
        specs=registry.list_tools(),
        audit=audit,
        environment={},
    )
    registry.configuration = configuration
    reader_token = "reader-key-" + "r" * 40
    verifier = StaticTokenVerifier(
        tokens={
            TOKEN: {
                "client_id": "harborrag-local",
                "sub": "harborrag-local",
                "role": "owner",
                "tenants": ["*"],
                "scopes": ["mcp:read", "mcp:admin"],
            },
            reader_token: {
                "client_id": "mcp-key:0",
                "sub": "user-demo",
                "role": "reader",
                "tenants": ["demo"],
                "scopes": ["mcp:read"],
            },
        },
        required_scopes=["mcp:read"],
    )
    server = create_mcp_server(registry=registry, auth=verifier)
    register_http_routes(
        server,
        mcp_path="/mcp",
        registry=registry,
        configuration=configuration,
        access=RouteAccess(verifier),
    )
    app = server.http_app(path="/mcp")
    reader = {"Authorization": f"Bearer {reader_token}"}

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            own_tools = await client.get("/api/tools", headers=reader)
            other_tools = await client.get("/api/tools?tenant_id=finance", headers=reader)
            own_call = await client.post(
                "/api/tools/call",
                headers=reader,
                json={"name": "list_sources", "arguments": {}},
            )
            other_call = await client.post(
                "/api/tools/call",
                headers=reader,
                json={"name": "list_sources", "arguments": {"tenant_id": "finance"}},
            )
            config = await client.get("/api/config", headers=reader)
            reload = await client.post("/api/config/reload", headers=reader)
            owner_config = await client.get("/api/config", headers=_owner_headers())

    assert own_tools.status_code == 200
    assert own_tools.json()["tenant_id"] == "demo"
    assert own_tools.json()["role"] == "reader"
    assert own_tools.json()["tenants"] == ["demo"]
    assert own_tools.json()["any_tenant"] is False
    assert other_tenants.lookups == 0
    assert [tool["name"] for tool in own_tools.json()["tools"]] == EXPECTED_READER_TOOLS
    assert other_tools.status_code == 403
    # Bound to the key's tenant (a runtime-less registry answers with a tool error, not 403).
    assert own_call.status_code == 200
    assert other_call.status_code == 403
    assert config.status_code == 403 and "reader key" in config.json()["error"]
    assert reload.status_code == 403
    assert owner_config.status_code == 200


_TOOL_FAILURES: dict[str, type[Exception]] = {
    "runtime": RuntimeError,
    "value": ValueError,
    "permission": PermissionError,
}


_TOOL_DETAIL = "corpus passage CANARY-7f3a quoted by the tool"
_CAUSE_DETAIL = "backend reply CANARY-91c2"


class _FailingTool(BaseTool):
    """Tool whose own execution fails with messages that quote what it read."""

    spec = ToolSpec(
        "failing_probe",
        "Raise an unexpected internal error in a test.",
        {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string"},
                "failure": {"type": "string", "enum": sorted(_TOOL_FAILURES)},
            },
            "required": ["tenant_id", "failure"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
    )

    async def call(self, arguments, *, principal_id):
        try:
            raise ConnectionError(_CAUSE_DETAIL)
        except ConnectionError as cause:
            raise _TOOL_FAILURES[arguments["failure"]](_TOOL_DETAIL) from cause


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", sorted(_TOOL_FAILURES))
async def test_tool_playground_masks_and_logs_a_failure_inside_the_tool(
    tmp_path, caplog, failure
) -> None:
    audit = McpAuditLog(path=tmp_path / "audit.jsonl")
    registry = McpServer(tools=[_FailingTool()], audit=audit)
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
        access=RouteAccess(verifier),
    )
    app = server.http_app(path="/mcp")

    with caplog.at_level(logging.ERROR, logger="harborrag.mcp.server.http"):
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.post(
                    "/api/tools/call",
                    headers=_owner_headers(),
                    json={
                        "name": "failing_probe",
                        "arguments": {"tenant_id": "demo", "failure": failure},
                    },
                )

    raised = _TOOL_FAILURES[failure]
    assert response.status_code == 500
    assert response.json()["error"] == f"tool execution failed ({raised.__name__})"
    assert "CANARY" not in response.text
    (record,) = [record for record in caplog.records if record.levelno >= logging.ERROR]
    logged = caplog.text
    assert "CANARY" not in logged
    assert record.exc_info is None
    assert "failing_probe" in logged
    assert raised.__name__ in logged
    assert "ConnectionError" in logged
    assert "test_http_server.py" in logged


class _TokenEchoingVerifier:
    """Verifier that fails with the presented token in its message."""

    async def verify_token(self, token: str) -> None:
        raise RuntimeError(f"key store rejected {token}")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failing_verifier_denies_the_request_and_keeps_the_token_out_of_the_log(
    tmp_path, caplog
) -> None:
    audit = McpAuditLog(path=tmp_path / "audit.jsonl")
    registry = McpServer(audit=audit)
    configuration = McpConfigurationStore(
        path=tmp_path / "mcp.yaml",
        configuration=McpConfiguration(),
        specs=registry.list_tools(),
        audit=audit,
        environment={},
    )
    server = create_mcp_server(registry=registry, auth=create_local_token_verifier(TOKEN))
    register_http_routes(
        server,
        mcp_path="/mcp",
        registry=registry,
        configuration=configuration,
        access=RouteAccess(_TokenEchoingVerifier()),
    )
    app = server.http_app(path="/mcp")

    with caplog.at_level(logging.DEBUG, logger="harborrag.mcp.server.http"):
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get("/api/tools", headers=_owner_headers())

    assert response.status_code == 401
    assert response.json()["error"] == "invalid bearer token"
    assert "RuntimeError" in caplog.text
    assert TOKEN not in caplog.text
    assert [record for record in caplog.records if record.exc_info] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_playground_refuses_a_body_nested_too_deep_to_parse(tmp_path) -> None:
    audit = McpAuditLog(path=tmp_path / "audit.jsonl")
    registry = McpServer(audit=audit)
    configuration = McpConfigurationStore(
        path=tmp_path / "mcp.yaml",
        configuration=McpConfiguration(),
        specs=registry.list_tools(),
        audit=audit,
        environment={},
    )
    verifier = create_local_token_verifier(TOKEN)
    server = create_mcp_server(registry=registry, auth=verifier)
    register_http_routes(
        server,
        mcp_path="/mcp",
        registry=registry,
        configuration=configuration,
        access=RouteAccess(verifier),
    )
    app = server.http_app(path="/mcp")
    depth = 50_000

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/api/tools/call",
                headers={**_owner_headers(), "Content-Type": "application/json"},
                content="[" * depth + "]" * depth,
            )

    assert response.status_code == 422
    assert audit.entries == []
