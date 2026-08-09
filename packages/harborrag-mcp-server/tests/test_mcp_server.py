from __future__ import annotations

import importlib.util
import json
from io import StringIO

import pytest

from harborrag_mcp_server.server import call_tool, create_mcp_server, list_tools
from harborrag_mcp_server.server.base import BaseMcpServer
from harborrag_mcp_server.server.server import McpServer
from harborrag_mcp_server.tools.base import BaseMcpTool, McpToolSpec
from harborrag_runtime.memory import InMemoryConversationMemory


def test_package_exposes_the_mcp_server_namespace() -> None:
    assert importlib.util.find_spec("harborrag_mcp_server") is not None


def test_module_check_lists_all_tools(tmp_path, monkeypatch, capsys) -> None:
    from harborrag_mcp_server.__main__ import main

    monkeypatch.chdir(tmp_path)
    assert main(["--check"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        "vector_search",
        "vector_search_advanced",
        "graph_triplet_search",
        "graph_path_search",
        "graph_subgraph_search",
        "graph_neighborhood",
    ]


def test_module_rejects_interactive_stdio_with_guidance(monkeypatch, capsys) -> None:
    import harborrag_mcp_server.__main__ as cli

    class InteractiveInput(StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(cli.sys, "stdin", InteractiveInput())

    with pytest.raises(SystemExit, match="2"):
        cli.main([])

    error = capsys.readouterr().err
    assert "must be launched by an MCP client" in error
    assert "--check" in error


def test_module_runs_stdio_when_launched_with_a_pipe(monkeypatch) -> None:
    import harborrag_mcp_server.__main__ as cli

    calls: list[tuple[str, bool]] = []

    class PipedInput(StringIO):
        def isatty(self) -> bool:
            return False

    class FakeTransport:
        def run(self, *, transport: str, show_banner: bool) -> None:
            calls.append((transport, show_banner))

    monkeypatch.setattr(cli.sys, "stdin", PipedInput())
    monkeypatch.setattr(cli, "_configured_memory", lambda _settings: InMemoryConversationMemory())
    monkeypatch.setattr(cli, "create_mcp_server", lambda **kwargs: FakeTransport())

    assert cli.main([]) == 0
    assert calls == [("stdio", False)]


def test_module_http_requires_a_strong_bearer_token(monkeypatch, capsys) -> None:
    import harborrag_mcp_server.__main__ as cli

    monkeypatch.delenv("HARBORRAG_MCP_BEARER_TOKEN", raising=False)

    with pytest.raises(SystemExit, match="2"):
        cli.main(["--transport", "http"])

    assert "HARBORRAG_MCP_BEARER_TOKEN" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_factory_registers_tools_on_real_fastmcp_transport(tmp_path, monkeypatch) -> None:
    fastmcp = pytest.importorskip("fastmcp")
    monkeypatch.setenv("HARBORRAG_MCP_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    with pytest.raises(RuntimeError, match="requires authentication"):
        create_mcp_server()
    with pytest.raises(ValueError, match="requires a runtime"):
        create_mcp_server(
            allow_unauthenticated_local=True,
            manage_runtime_lifecycle=True,
        )

    transport = create_mcp_server(allow_unauthenticated_local=True)
    async with fastmcp.Client(transport) as client:
        tools = await client.list_tools()

    assert type(transport).__module__.startswith("fastmcp.")
    assert [tool.name for tool in tools] == [
        "vector_search",
        "vector_search_advanced",
        "graph_triplet_search",
        "graph_path_search",
        "graph_subgraph_search",
        "graph_neighborhood",
    ]
    assert tools[0].inputSchema["required"] == ["query", "tenant_id"]


class BrokenTool(BaseMcpTool):
    spec = McpToolSpec("broken", "broken")

    async def call(self, arguments, *, principal_id):
        return await super().call(arguments, principal_id=principal_id)


class BrokenServer(BaseMcpServer):
    def list_tools(self):
        return super().list_tools()

    async def call_tool(self, name, arguments=None, *, principal_id="in-process"):
        return await super().call_tool(name, arguments, principal_id=principal_id)


@pytest.mark.asyncio
async def test_mcp_base_methods_raise():
    with pytest.raises(NotImplementedError):
        await BrokenTool().call({}, principal_id="test")
    with pytest.raises(NotImplementedError):
        BrokenServer().list_tools()
    with pytest.raises(NotImplementedError):
        await BrokenServer().call_tool("x")


@pytest.mark.asyncio
async def test_mcp_registry_exposes_retrieval_tools():
    spec = McpToolSpec("tool", "description")
    assert spec.input_schema == {"type": "object"}
    server = McpServer()
    expected = [
        "vector_search",
        "vector_search_advanced",
        "graph_triplet_search",
        "graph_path_search",
        "graph_subgraph_search",
        "graph_neighborhood",
    ]
    assert [tool.name for tool in server.list_tools()] == expected
    assert [item["name"] for item in list_tools()] == expected
    assert (
        await server.call_tool(
            "vector_search",
            {"query": "harbor", "tenant_id": "demo"},
        )
    )["ok"] is False
    with pytest.raises(ValueError):
        await server.call_tool("missing")
    with pytest.raises(ValueError):
        await call_tool("missing")


def test_audit_log_records_tool_calls():
    from harborrag_mcp_server.audit import McpAuditLog

    log = McpAuditLog()
    invocation_id = log.start(
        "harborrag_health_check",
        {},
        principal_id="subject-1",
    )
    log.finish(
        invocation_id,
        "harborrag_health_check",
        principal_id="subject-1",
        outcome="success",
    )

    assert [entry["event"] for entry in log.entries] == [
        "tool_invocation_attempted",
        "tool_invocation_completed",
    ]
    assert {entry["principal_id"] for entry in log.entries} == {"subject-1"}
    assert log.entries[0]["arguments_sha256"]
    assert log.entries[1]["outcome"] == "success"


def test_tool_policy_enforces_result_budget():
    from harborrag_mcp_server.policy import McpToolPolicy

    policy = McpToolPolicy(max_results=2)
    policy.check_results(2)
    with pytest.raises(ValueError, match="MCP result budget exceeded"):
        policy.check_results(3)

    default_policy = McpToolPolicy()
    assert default_policy.max_results == 20
    assert default_policy.allow_ingestion is False


def test_tool_policy_enforces_declared_input_schema():
    from harborrag_mcp_server.policy import McpToolPolicy

    spec = McpToolSpec(
        "search",
        "Search.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    policy = McpToolPolicy()

    policy.check_call(spec, {"query": "release"})
    with pytest.raises(ValueError, match="do not match"):
        policy.check_call(spec, {})
    with pytest.raises(ValueError, match="do not match"):
        policy.check_call(spec, {"query": "release", "token": "not-allowed"})


@pytest.mark.asyncio
async def test_call_tool_facade_records_an_audit_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real call_tool facade (not McpAuditLog called in isolation) must
    leave an audit trail: McpServer.call_tool wires McpAuditLog.record
    into the actual call path exercised by the module-level facade."""
    import harborrag_mcp_server.server.server as server_module
    from harborrag_mcp_server.audit import McpAuditLog

    fresh_audit = McpAuditLog()
    monkeypatch.setattr(server_module, "_default_audit_log", fresh_audit)

    result = await call_tool(
        "vector_search",
        {"query": "harbor", "tenant_id": "demo"},
    )

    assert result["ok"] is False
    assert [entry["event"] for entry in fresh_audit.entries] == [
        "tool_invocation_attempted",
        "tool_invocation_completed",
    ]
    assert fresh_audit.entries[-1]["outcome"] == "error"
    assert fresh_audit.entries[-1]["error_type"] == "ToolReportedError"


@pytest.mark.asyncio
async def test_call_tool_records_audit_entry_even_when_tool_raises() -> None:
    """A tool that raises mid-call must still leave an audit trail: the audit
    record happens before tool.call(), not after a successful result."""
    from harborrag_mcp_server.audit import McpAuditLog
    from harborrag_mcp_server.policy import McpToolPolicy

    server = McpServer(tools=[BrokenTool()], policy=McpToolPolicy(), audit=McpAuditLog())

    with pytest.raises(NotImplementedError):
        await server.call_tool("broken")

    assert [entry["event"] for entry in server.audit.entries] == [
        "tool_invocation_attempted",
        "tool_invocation_completed",
    ]
    assert server.audit.entries[-1]["outcome"] == "error"
    assert server.audit.entries[-1]["error_type"] == "NotImplementedError"


@pytest.mark.asyncio
async def test_call_tool_facade_rejects_policy_violation_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy configured to reject any non-empty result must actually
    reject a call made through call_tool (server.call_tool -> policy check),
    not just when McpToolPolicy.check_results is invoked directly -- and the
    rejected call must still be audited."""
    import harborrag_mcp_server.server.server as server_module
    from harborrag_mcp_server.audit import McpAuditLog
    from harborrag_mcp_server.policy import McpToolPolicy

    strict_policy = McpToolPolicy(max_results=0)
    fresh_audit = McpAuditLog()
    monkeypatch.setattr(server_module, "_default_policy", strict_policy)
    monkeypatch.setattr(server_module, "_default_audit_log", fresh_audit)

    with pytest.raises(ValueError, match="MCP result budget exceeded"):
        await call_tool(
            "vector_search",
            {"query": "harbor", "tenant_id": "demo"},
        )

    assert fresh_audit.entries[-1]["outcome"] == "error"
    assert fresh_audit.entries[-1]["error_type"] == "ValueError"


def test_request_principal_requires_owner_role(monkeypatch) -> None:
    from types import SimpleNamespace

    import fastmcp.server.dependencies as dependencies

    from harborrag_mcp_server.server import _request_principal_id

    reader = SimpleNamespace(claims={"sub": "reader-1", "role": "reader"}, client_id="client")
    monkeypatch.setattr(dependencies, "get_access_token", lambda: reader)
    with pytest.raises(PermissionError, match="owner"):
        _request_principal_id()

    owner = SimpleNamespace(
        claims={"sub": "owner-1", "role": "owner", "tenants": ["demo"]},
        client_id="client",
    )
    monkeypatch.setattr(dependencies, "get_access_token", lambda: owner)
    assert _request_principal_id("demo") == "owner-1"
    with pytest.raises(PermissionError, match="requested tenant"):
        _request_principal_id("other")

    global_owner = SimpleNamespace(
        claims={"sub": "global-owner", "role": "owner", "tenants": ["*"]},
        client_id="client",
    )
    monkeypatch.setattr(dependencies, "get_access_token", lambda: global_owner)
    assert _request_principal_id("other") == "global-owner"


def test_tenant_scoped_owner_cannot_access_global_configuration() -> None:
    from types import SimpleNamespace

    from harborrag_mcp_server.server.http_auth import Unauthorized, authorize_request_tenant

    request = SimpleNamespace(state=SimpleNamespace(allowed_tenants=frozenset({"demo"})))
    authorize_request_tenant(request, "demo")
    with pytest.raises(Unauthorized, match="requested tenant"):
        authorize_request_tenant(request, "*")
