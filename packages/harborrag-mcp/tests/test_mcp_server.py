from __future__ import annotations

import pytest

from harborrag_mcp.server import call_tool, list_tools
from harborrag_mcp.server.base import BaseMcpServer
from harborrag_mcp.server.server import McpServer
from harborrag_mcp.tools.base import BaseMcpTool, McpToolSpec
from harborrag_mcp.tools.health import HealthTool


class BrokenTool(BaseMcpTool):
    spec = McpToolSpec("broken", "broken")

    def call(self, arguments):
        return super().call(arguments)


class BrokenServer(BaseMcpServer):
    def list_tools(self):
        return super().list_tools()

    def call_tool(self, name, arguments=None):
        return super().call_tool(name, arguments)


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
    from harborrag_mcp.audit import McpAuditLog

    log = McpAuditLog()
    log.record("harborrag_health_check")
    log.record("harborrag_retrieve")

    assert log.entries == [
        {"tool": "harborrag_health_check"},
        {"tool": "harborrag_retrieve"},
    ]


def test_tool_policy_enforces_result_budget():
    from harborrag_mcp.policy import McpToolPolicy

    policy = McpToolPolicy(max_results=2)
    policy.check_results(2)
    with pytest.raises(ValueError, match="MCP result budget exceeded"):
        policy.check_results(3)

    default_policy = McpToolPolicy()
    assert default_policy.max_results == 20
    assert default_policy.allow_ingestion is False


def test_tool_schema_builds_input_schema_stub():
    from harborrag_mcp.schemas import tool_schema

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
    import harborrag_mcp.server.server as server_module
    from harborrag_mcp.audit import McpAuditLog

    fresh_audit = McpAuditLog()
    monkeypatch.setattr(server_module, "_default_audit_log", fresh_audit)

    result = call_tool("harborrag_health_check")

    assert result["ok"] is True
    assert fresh_audit.entries == [{"tool": "harborrag_health_check"}]


def test_call_tool_records_audit_entry_even_when_tool_raises() -> None:
    """A tool that raises mid-call must still leave an audit trail: the audit
    record happens before tool.call(), not after a successful result."""
    from harborrag_mcp.audit import McpAuditLog
    from harborrag_mcp.policy import McpToolPolicy

    server = McpServer(tools=[BrokenTool()], policy=McpToolPolicy(), audit=McpAuditLog())

    with pytest.raises(NotImplementedError):
        server.call_tool("broken")

    assert server.audit.entries == [{"tool": "broken"}]


def test_call_tool_facade_rejects_policy_violation_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy configured to reject any non-empty result must actually
    reject a call made through call_tool (server.call_tool -> policy check),
    not just when McpToolPolicy.check_results is invoked directly -- and the
    rejected call must still be audited."""
    import harborrag_mcp.server.server as server_module
    from harborrag_mcp.audit import McpAuditLog
    from harborrag_mcp.policy import McpToolPolicy

    strict_policy = McpToolPolicy(max_results=0)
    fresh_audit = McpAuditLog()
    monkeypatch.setattr(server_module, "_default_policy", strict_policy)
    monkeypatch.setattr(server_module, "_default_audit_log", fresh_audit)

    with pytest.raises(ValueError, match="MCP result budget exceeded"):
        call_tool("harborrag_health_check")

    # The rejected invocation is still recorded.
    assert fresh_audit.entries == [{"tool": "harborrag_health_check"}]
