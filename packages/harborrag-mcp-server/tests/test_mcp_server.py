from __future__ import annotations

import asyncio
import importlib.util

import pytest

from harborrag_mcp_server.server import call_tool, create_mcp_server, list_tools
from harborrag_mcp_server.server.base import BaseMcpServer
from harborrag_mcp_server.server.server import McpServer
from harborrag_mcp_server.tools.base import BaseMcpTool, McpToolSpec
from harborrag_mcp_server.tools.health import HealthTool


def test_package_uses_the_mcp_server_namespace_without_an_old_alias() -> None:
    assert importlib.util.find_spec("harborrag_mcp_server") is not None
    assert importlib.util.find_spec("harborrag_mcp") is None


def test_factory_registers_tools_on_real_fastmcp_transport(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HARBORRAG_MCP_AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    with pytest.raises(RuntimeError, match="requires authentication"):
        create_mcp_server()

    transport = create_mcp_server(allow_unauthenticated_local=True)
    tools = asyncio.run(transport.list_tools())  # type: ignore[attr-defined]

    assert type(transport).__module__.startswith("fastmcp.")
    assert [tool.name for tool in tools] == ["harborrag_health_check"]
    assert tools[0].parameters == {
        "type": "object",
        "additionalProperties": False,
    }


class BrokenTool(BaseMcpTool):
    spec = McpToolSpec("broken", "broken")

    def call(self, arguments):
        return super().call(arguments)


class BrokenServer(BaseMcpServer):
    def list_tools(self):
        return super().list_tools()

    def call_tool(self, name, arguments=None, *, principal_id="in-process"):
        return super().call_tool(name, arguments, principal_id=principal_id)


def test_mcp_base_methods_raise():
    with pytest.raises(NotImplementedError):
        BrokenTool().call({})
    with pytest.raises(NotImplementedError):
        BrokenServer().list_tools()
    with pytest.raises(NotImplementedError):
        BrokenServer().call_tool("x")


def test_mcp_health_tool_server_and_module_facade():
    spec = McpToolSpec("tool", "description")
    assert spec.input_schema == {"type": "object"}
    health = HealthTool().call({})
    assert health["ok"] is True
    server = McpServer()
    assert [tool.name for tool in server.list_tools()] == ["harborrag_health_check"]
    assert server.call_tool("harborrag_health_check")["ok"] is True
    with pytest.raises(ValueError):
        server.call_tool("missing")
    assert list_tools()[0]["name"] == "harborrag_health_check"
    assert call_tool("harborrag_health_check")["ok"] is True
    with pytest.raises(ValueError):
        call_tool("missing")


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


def test_tool_schema_builds_input_schema_stub():
    from harborrag_mcp_server.schemas import tool_schema

    schema = tool_schema("harborrag_health_check", "Return diagnostics.")

    assert schema == {
        "name": "harborrag_health_check",
        "description": "Return diagnostics.",
        "inputSchema": {"type": "object"},
    }


def test_call_tool_facade_records_an_audit_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real call_tool facade (not McpAuditLog called in isolation) must
    leave an audit trail: McpServer.call_tool wires McpAuditLog.record
    into the actual call path exercised by the module-level facade."""
    import harborrag_mcp_server.server.server as server_module
    from harborrag_mcp_server.audit import McpAuditLog

    fresh_audit = McpAuditLog()
    monkeypatch.setattr(server_module, "_default_audit_log", fresh_audit)

    result = call_tool("harborrag_health_check")

    assert result["ok"] is True
    assert [entry["event"] for entry in fresh_audit.entries] == [
        "tool_invocation_attempted",
        "tool_invocation_completed",
    ]
    assert fresh_audit.entries[-1]["outcome"] == "success"


def test_call_tool_records_audit_entry_even_when_tool_raises() -> None:
    """A tool that raises mid-call must still leave an audit trail: the audit
    record happens before tool.call(), not after a successful result."""
    from harborrag_mcp_server.audit import McpAuditLog
    from harborrag_mcp_server.policy import McpToolPolicy

    server = McpServer(tools=[BrokenTool()], policy=McpToolPolicy(), audit=McpAuditLog())

    with pytest.raises(NotImplementedError):
        server.call_tool("broken")

    assert [entry["event"] for entry in server.audit.entries] == [
        "tool_invocation_attempted",
        "tool_invocation_completed",
    ]
    assert server.audit.entries[-1]["outcome"] == "error"
    assert server.audit.entries[-1]["error_type"] == "NotImplementedError"


def test_call_tool_facade_rejects_policy_violation_end_to_end(
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
        call_tool("harborrag_health_check")

    assert fresh_audit.entries[-1]["outcome"] == "error"
    assert fresh_audit.entries[-1]["error_type"] == "ValueError"


def test_audit_memory_is_bounded_and_never_stores_arguments() -> None:
    from harborrag_mcp_server.audit import McpAuditLog

    log = McpAuditLog(max_entries=2)
    for index in range(3):
        log.start("tool", {"secret": f"value-{index}"}, principal_id="subject")

    assert len(log.entries) == 2
    assert all("arguments_sha256" in entry for entry in log.entries)
    assert "value-" not in repr(log.entries)
