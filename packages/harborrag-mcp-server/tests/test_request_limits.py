from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from harborrag_engine.tools.base import BaseTool, ToolSpec
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.configuration import McpConfiguration, McpConfigurationStore
from harborrag_mcp_server.server import McpServer, create_mcp_server
from harborrag_mcp_server.server.http import register_health_route, register_http_routes
from harborrag_mcp_server.server.request_limits import (
    CredentialLimiter,
    RequestLimitExceeded,
    RequestLimits,
    request_limits_from_environment,
)

OWNER_TOKEN = "owner-token-" + "o" * 40
READER_TOKEN = "reader-key-" + "r" * 40
_VERIFIER = StaticTokenVerifier(
    tokens={
        OWNER_TOKEN: {
            "client_id": "harborrag-local",
            "sub": "harborrag-local",
            "role": "owner",
            "tenants": ["*"],
            "scopes": ["mcp:read", "mcp:admin"],
        },
        READER_TOKEN: {
            "client_id": "mcp-key:0a1b2c",
            "sub": "user-demo",
            "role": "reader",
            "tenants": ["demo"],
            "scopes": ["mcp:read"],
        },
    },
    required_scopes=["mcp:read"],
)


class _FrozenClock:
    """Monotonic clock that moves only when a test advances it."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _GatedTool(BaseTool):
    """Tool that holds its call open until the test releases it."""

    spec = ToolSpec(
        "gated_probe",
        "Wait until the test releases the call.",
        {
            "type": "object",
            "properties": {"tenant_id": {"type": "string"}},
            "required": ["tenant_id"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
    )

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def call(self, arguments, *, principal_id):
        self.entered.set()
        await self.release.wait()
        return {"ok": True}


def _registry(tmp_path: Path, tools: list[BaseTool] | None = None) -> McpServer:
    audit = McpAuditLog(path=tmp_path / "audit.jsonl")
    registry = McpServer(tools=tools, audit=audit) if tools else McpServer(audit=audit)
    registry.configuration = McpConfigurationStore(
        path=tmp_path / "mcp.yaml",
        configuration=McpConfiguration(),
        specs=registry.list_tools(),
        audit=audit,
        environment={},
    )
    return registry


@asynccontextmanager
async def _client(
    tmp_path: Path, limiter: CredentialLimiter, tools: list[BaseTool] | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    registry = _registry(tmp_path, tools)
    server = create_mcp_server(registry=registry, auth=_VERIFIER)
    register_http_routes(
        server,
        mcp_path="/mcp",
        registry=registry,
        configuration=registry.configuration,
        token_verifier=_VERIFIER,
        limiter=limiter,
    )
    app = server.http_app(path="/mcp")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _mcp_session(client: httpx.AsyncClient, token: str) -> dict[str, str]:
    headers = {
        **_bearer(token),
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    response = await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "harborrag-test", "version": "1"},
            },
        },
    )
    assert response.status_code == 200
    return {
        **headers,
        "mcp-session-id": response.headers["mcp-session-id"],
        "mcp-protocol-version": "2025-06-18",
    }


async def _list_tools_over_mcp(client: httpx.AsyncClient, headers: dict[str, str]) -> object:
    response = await client.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    assert response.status_code == 200
    data = next(line for line in response.text.splitlines() if line.startswith("data: "))
    return json.loads(data.removeprefix("data: "))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_limiter_refuses_past_the_burst_and_admits_again_once_tokens_refill() -> None:
    clock = _FrozenClock()
    limiter = CredentialLimiter(RequestLimits(requests_per_second=2, max_in_flight=0), clock=clock)

    for _ in range(2):
        async with limiter.admit("mcp-key:a"):
            pass
    with pytest.raises(RequestLimitExceeded) as refused:
        async with limiter.admit("mcp-key:a"):
            pass
    async with limiter.admit("mcp-key:b"):
        pass
    clock.now += 0.5
    async with limiter.admit("mcp-key:a"):
        pass

    assert refused.value.retry_after_seconds == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_limiter_refuses_a_request_over_the_in_flight_cap_without_waiting() -> None:
    limiter = CredentialLimiter(RequestLimits(requests_per_second=0, max_in_flight=1))

    async with limiter.admit("mcp-key:a"):
        with pytest.raises(RequestLimitExceeded) as refused:
            async with limiter.admit("mcp-key:a"):
                pass
        async with limiter.admit("mcp-key:b"):
            pass
    async with limiter.admit("mcp-key:a"):
        pass

    assert refused.value.retry_after_seconds == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_limiter_with_both_limits_at_zero_admits_everything() -> None:
    limiter = CredentialLimiter(
        RequestLimits(requests_per_second=0, max_in_flight=0), clock=_FrozenClock()
    )

    async with limiter.admit("mcp-key:a"), limiter.admit("mcp-key:a"):
        for _ in range(100):
            async with limiter.admit("mcp-key:a"):
                pass


@pytest.mark.unit
@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({}, RequestLimits(requests_per_second=10, max_in_flight=4)),
        (
            {"HARBORRAG_MCP_RATE_LIMIT": "25", "HARBORRAG_MCP_MAX_INFLIGHT": " 8 "},
            RequestLimits(requests_per_second=25, max_in_flight=8),
        ),
        (
            {"HARBORRAG_MCP_RATE_LIMIT": "0", "HARBORRAG_MCP_MAX_INFLIGHT": "0"},
            RequestLimits(requests_per_second=0, max_in_flight=0),
        ),
    ],
)
def test_request_limits_come_from_the_environment_with_the_http_api_defaults(
    environment: dict[str, str], expected: RequestLimits
) -> None:
    assert request_limits_from_environment(environment) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("variable", "value"),
    [("HARBORRAG_MCP_RATE_LIMIT", "-1"), ("HARBORRAG_MCP_MAX_INFLIGHT", "four")],
)
def test_request_limits_refuse_a_value_that_is_not_a_count(variable: str, value: str) -> None:
    with pytest.raises(ValueError, match=variable):
        request_limits_from_environment({variable: value})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_tools_answers_429_with_retry_after_and_leaves_status_and_health_open(
    tmp_path: Path,
) -> None:
    limiter = CredentialLimiter(
        RequestLimits(requests_per_second=2, max_in_flight=0), clock=_FrozenClock()
    )

    async with _client(tmp_path, limiter) as client:
        listed = await client.get("/api/tools", headers=_bearer(OWNER_TOKEN))
        called = await client.post(
            "/api/tools/call",
            headers=_bearer(OWNER_TOKEN),
            json={"name": "list_sources", "arguments": {"tenant_id": "demo"}},
        )
        refused = await client.get("/api/tools", headers=_bearer(OWNER_TOKEN))
        other_credential = await client.get("/api/tools", headers=_bearer(READER_TOKEN))
        status_page = await client.get("/")
        health = await client.get("/healthz")

    assert listed.status_code == 200
    assert called.status_code == 200
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "1"
    assert "retry after 1 s" in refused.json()["error"]
    assert other_credential.status_code == 200
    assert status_page.status_code == 200
    assert health.status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mcp_and_api_tools_share_one_budget_and_mcp_gets_the_rate_limit_error(
    tmp_path: Path,
) -> None:
    limiter = CredentialLimiter(
        RequestLimits(requests_per_second=3, max_in_flight=0), clock=_FrozenClock()
    )

    async with _client(tmp_path, limiter) as client:
        session = await _mcp_session(client, READER_TOKEN)
        listed = await _list_tools_over_mcp(client, session)
        playground = await client.get("/api/tools", headers=_bearer(READER_TOKEN))
        refused = await _list_tools_over_mcp(client, session)
        refused_playground = await client.get("/api/tools", headers=_bearer(READER_TOKEN))

    assert "result" in listed
    assert playground.status_code == 200
    assert refused == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {"code": -32000, "message": "request limit exceeded; retry after 1 s"},
    }
    assert refused_playground.status_code == 429


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_tools_refuses_a_call_over_the_in_flight_cap(tmp_path: Path) -> None:
    tool = _GatedTool()
    limiter = CredentialLimiter(RequestLimits(requests_per_second=0, max_in_flight=1))
    call = {"name": "gated_probe", "arguments": {"tenant_id": "demo"}}

    async with _client(tmp_path, limiter, tools=[tool]) as client:
        held = asyncio.create_task(
            client.post("/api/tools/call", headers=_bearer(OWNER_TOKEN), json=call)
        )
        await asyncio.wait_for(tool.entered.wait(), timeout=5)
        refused = await client.post("/api/tools/call", headers=_bearer(OWNER_TOKEN), json=call)
        tool.release.set()
        finished = await held
        after = await client.post("/api/tools/call", headers=_bearer(OWNER_TOKEN), json=call)

    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "1"
    assert finished.status_code == 200
    assert after.status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_explorer_server_limits_its_mcp_endpoint(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    server = create_mcp_server(registry=registry, auth=_VERIFIER)
    register_health_route(
        server,
        mcp_path="/mcp",
        tool_names=[],
        service="harborrag-mcp-ui",
        limiter=CredentialLimiter(
            RequestLimits(requests_per_second=1, max_in_flight=0), clock=_FrozenClock()
        ),
    )
    app = server.http_app(path="/mcp")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            session = await _mcp_session(client, READER_TOKEN)
            refused = await _list_tools_over_mcp(client, session)
            health = await client.get("/healthz")

    assert refused["error"]["code"] == -32000
    assert health.status_code == 200


@pytest.mark.unit
def test_the_launcher_refuses_a_rate_limit_that_is_not_a_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import harborrag_mcp_server.__main__ as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HARBORRAG_MCP_RATE_LIMIT", "fast")

    with pytest.raises(SystemExit, match="2"):
        cli.main(["--check"])

    assert "HARBORRAG_MCP_RATE_LIMIT" in capsys.readouterr().err


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_launcher_applies_the_configured_limits_to_the_tool_routes(
    tmp_path: Path,
) -> None:
    import harborrag_mcp_server.__main__ as cli

    registry = _registry(tmp_path)
    server = create_mcp_server(registry=registry, auth=_VERIFIER)
    arguments = argparse.Namespace(
        host="127.0.0.1",
        port=8010,
        path="/mcp",
        request_limits=request_limits_from_environment({"HARBORRAG_MCP_RATE_LIMIT": "1"}),
    )
    cli._register_routes(server, arguments, registry, registry.configuration, _VERIFIER, ui=False)
    app = server.http_app(path="/mcp")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            statuses = [
                (await client.get("/api/tools", headers=_bearer(OWNER_TOKEN))).status_code
                for _ in range(3)
            ]

    assert statuses[0] == 200
    assert 429 in statuses
