from __future__ import annotations

import contextlib
import json
from io import StringIO
from pathlib import Path

import pytest
from catalog_support import EXPECTED_READER_TOOLS

from harborrag_mcp_server.server import call_tool, create_mcp_server, list_tools
from harborrag_mcp_server.server.base import BaseMcpServer
from harborrag_mcp_server.server.server import McpServer
from harborrag_runtime.memory import InMemoryConversationMemory
from harborrag_runtime.tools.base import BaseTool, ToolSpec


def test_package_exposes_the_mcp_server_namespace() -> None:
    import harborrag_mcp_server

    assert harborrag_mcp_server.create_mcp_server is create_mcp_server


def test_module_check_lists_all_tools(tmp_path, monkeypatch, capsys) -> None:
    from harborrag_mcp_server.__main__ import main

    monkeypatch.chdir(tmp_path)
    assert main(["--check"]) == 0
    assert json.loads(capsys.readouterr().out) == EXPECTED_READER_TOOLS


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
    assert [tool.name for tool in tools] == EXPECTED_READER_TOOLS
    assert tools[0].inputSchema["required"] == ["query", "tenant_id"]

    path = next(tool for tool in tools if tool.name == "graph_path_search")
    assert path.inputSchema["additionalProperties"] is False
    assert set(path.inputSchema["properties"]) == {
        "start_node",
        "end_node",
        "relationship_types",
        "direction",
        "max_depth",
        "max_paths",
        "tenant_id",
    }
    assert path.annotations is not None
    assert path.annotations.readOnlyHint is True
    assert path.annotations.destructiveHint is False
    assert path.annotations.idempotentHint is True
    assert path.annotations.openWorldHint is False
    assert path.outputSchema is not None

    describe = next(tool for tool in tools if tool.name == "describe_graph")
    assert describe.inputSchema["additionalProperties"] is False
    assert set(describe.inputSchema["properties"]) == set()
    assert describe.annotations is not None
    assert describe.annotations.readOnlyHint is True
    assert describe.annotations.destructiveHint is False
    assert describe.annotations.idempotentHint is True
    assert describe.annotations.openWorldHint is False
    assert describe.outputSchema is not None


class BrokenTool(BaseTool):
    spec = ToolSpec("broken", "broken", output_schema={"type": "object"})

    async def call(self, arguments, *, principal_id):
        return await super().call(arguments, principal_id=principal_id)


class BrokenServer(BaseMcpServer):
    def list_tools(self):
        return super().list_tools()

    async def call_tool(self, name, arguments=None, *, principal_id="in-process"):
        return await super().call_tool(name, arguments, principal_id=principal_id)


class InvalidOutputTool(BaseTool):
    spec = ToolSpec(
        "invalid_output",
        "Return an invalid result.",
        output_schema={
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"const": True}},
            "additionalProperties": False,
        },
    )

    async def call(self, arguments, *, principal_id):
        del arguments, principal_id
        return {"ok": False}


@pytest.mark.asyncio
async def test_mcp_base_methods_raise():
    with pytest.raises(NotImplementedError):
        await BrokenTool().call({}, principal_id="test")
    with pytest.raises(NotImplementedError):
        BrokenServer().list_tools()
    with pytest.raises(NotImplementedError):
        await BrokenServer().call_tool("x")


@pytest.mark.asyncio
async def test_server_rejects_tool_output_that_breaks_its_advertised_schema() -> None:
    server = McpServer(tools=[InvalidOutputTool()])

    with pytest.raises(RuntimeError, match="invalid output"):
        await server.call_tool("invalid_output")


@pytest.mark.asyncio
async def test_mcp_registry_exposes_retrieval_tools():
    spec = ToolSpec("tool", "description")
    assert spec.input_schema == {"type": "object"}
    server = McpServer()
    assert [tool.name for tool in server.list_tools()] == EXPECTED_READER_TOOLS
    assert [item["name"] for item in list_tools()] == EXPECTED_READER_TOOLS
    result = await server.call_tool(
        "vector_search",
        {"query": "harbor", "tenant_id": "demo"},
    )
    assert result == {"ok": False, "error": "vector retrieval backend is not configured"}
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

    spec = ToolSpec(
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


def test_request_principal_requires_reader_or_owner_role(monkeypatch) -> None:
    from types import SimpleNamespace

    dependencies = pytest.importorskip("fastmcp.server.dependencies")

    from harborrag_mcp_server.server import _request_principal_id

    reader = SimpleNamespace(
        claims={"sub": "reader-1", "role": "reader", "tenants": ["demo"]}, client_id="client"
    )
    monkeypatch.setattr(dependencies, "get_access_token", lambda: reader)
    assert _request_principal_id("demo") == "reader-1"
    with pytest.raises(PermissionError, match="requested tenant"):
        _request_principal_id("other")

    invalid = SimpleNamespace(claims={"sub": "guest-1", "role": "guest"}, client_id="client")
    monkeypatch.setattr(dependencies, "get_access_token", lambda: invalid)
    with pytest.raises(PermissionError, match="reader or owner"):
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


def test_the_transport_masks_details_of_an_unexpected_failure() -> None:
    """A driver or filesystem error must not be narrated to the MCP client.

    FastMCP defaults ``mask_error_details`` off, which relays any exception
    escaping a tool as ``Error calling tool 'x': {exc}`` -- enough to leak a
    connection URL or a server path. The HTTP route already masks; the MCP
    transport has to agree.
    """

    pytest.importorskip("fastmcp")

    transport = create_mcp_server(allow_unauthenticated_local=True)

    # FastMCP keeps the resolved setting private; there is no public accessor.
    assert transport._mask_error_details is True


@pytest.mark.asyncio
async def test_a_refused_request_is_audited_before_it_is_rejected(monkeypatch) -> None:
    """A token probing another tenant is exactly what the trail exists to show.

    Resolving the principal as a call argument put it before ``call_tool``, so
    an authorization refusal produced no audit record at all.
    """

    from types import SimpleNamespace

    dependencies = pytest.importorskip("fastmcp.server.dependencies")

    from harborrag_mcp_server.audit import McpAuditLog
    from harborrag_mcp_server.server import _tool_handler

    intruder = SimpleNamespace(
        claims={"sub": "reader-1", "role": "reader", "tenants": ["demo"]},
        client_id="client",
    )
    monkeypatch.setattr(dependencies, "get_access_token", lambda: intruder)
    server = McpServer(audit=McpAuditLog())
    handler = _tool_handler(server, "describe_graph")

    with pytest.raises(PermissionError):
        await handler(tenant_id="someone-else")

    entries = server.audit.entries
    assert [entry["event"] for entry in entries] == [
        "tool_invocation_attempted",
        "tool_invocation_completed",
    ]
    assert entries[-1]["error_type"] == "PermissionError"
    assert entries[-1]["outcome"] == "error"
    assert all(entry["principal_id"] == "reader-1" for entry in entries)


@pytest.mark.asyncio
async def test_the_audit_records_which_tenant_a_call_touched() -> None:
    """The first question asked of a trail, and it could not answer it."""

    from harborrag_mcp_server.audit import McpAuditLog

    log = McpAuditLog()
    server = McpServer(audit=log)

    await server.call_tool("describe_graph", {}, principal_id="reader-1")
    with contextlib.suppress(Exception):
        # Rejected for a missing query; the point is that the attempt is
        # recorded against the tenant it was aimed at.
        await server.call_tool("vector_search", {"tenant_id": "  ACME  "}, principal_id="reader-1")

    tenants = [entry["tenant_id"] for entry in log.entries]
    # describe_graph takes no tenant; vector_search records the canonical
    # stripped value, the same one that drove policy and validation.
    assert tenants[0] is None
    assert "ACME" in tenants


def test_a_relative_audit_path_does_not_follow_the_launch_directory(tmp_path, monkeypatch) -> None:
    """An MCP client picks the working directory; the trail must not move.

    A relative path also meant the owner-only directory rules the writer
    enforces landed wherever the client happened to start the server.
    """

    from harborrag_mcp_server.audit import McpAuditLog

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    log = McpAuditLog(path=Path(".harborrag/mcp-audit.jsonl"))

    assert log.path is not None
    assert log.path.is_absolute()
    assert log.path == tmp_path / ".harborrag/mcp-audit.jsonl"


def test_an_absolute_audit_path_is_left_alone(tmp_path) -> None:
    from harborrag_mcp_server.audit import McpAuditLog

    chosen = tmp_path / "audit.jsonl"

    assert McpAuditLog(path=chosen).path == chosen


def test_one_predicate_decides_whether_a_result_failed() -> None:
    """The audit and the MCP handler disagreed about status == "error"."""

    from harborrag_mcp_server.server.base import tool_reported_error

    assert tool_reported_error({"ok": False}) is True
    assert tool_reported_error({"status": "error"}) is True
    assert tool_reported_error({"ok": True}) is False
    assert tool_reported_error({"results": []}) is False
