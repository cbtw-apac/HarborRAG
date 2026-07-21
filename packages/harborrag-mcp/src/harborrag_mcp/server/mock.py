from __future__ import annotations

from dataclasses import dataclass, field

from harborrag_mcp.audit import McpAuditLog
from harborrag_mcp.policy import McpToolPolicy
from harborrag_mcp.server.base import BaseMcpServer
from harborrag_mcp.tools.base import BaseMcpTool, McpToolSpec
from harborrag_mcp.tools.mock import MockHealthTool, MockRetrieveTool

# Shared, process-wide default policy/audit singletons. The module-level
# call_tool/list_tools facade in harborrag_mcp.server.__init__ constructs a
# fresh MockMcpServer per invocation, so these live outside the dataclass
# defaults (referenced via a lambda, not instantiated per-instance) to give
# every facade call a durable audit trail and a consistent policy instead of
# a policy/audit object that is discarded immediately after each call.
_default_policy = McpToolPolicy()
_default_audit_log = McpAuditLog()


def _result_count(result: dict[str, object]) -> int:
    """Best-effort item count for a tool result, for policy budget checks.

    Tools that return a `results` list (e.g. retrieval) are counted by list
    length; single-payload tools (e.g. health checks) count as one result.
    """
    results = result.get("results")
    if isinstance(results, list):
        return len(results)
    return 1


@dataclass(slots=True)
class MockMcpServer(BaseMcpServer):
    tools: list[BaseMcpTool] = field(default_factory=lambda: [MockHealthTool(), MockRetrieveTool()])
    policy: McpToolPolicy = field(default_factory=lambda: _default_policy)
    audit: McpAuditLog = field(default_factory=lambda: _default_audit_log)

    def list_tools(self) -> list[McpToolSpec]:
        return [tool.spec for tool in self.tools]

    def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
        for tool in self.tools:
            if tool.spec.name == name:
                # Audit every invocation of a known tool before it runs, so a
                # tool that raises (or a policy check that later rejects the
                # result) still leaves a record that the call happened.
                self.audit.record(name)
                result = tool.call(arguments or {})
                self.policy.check_results(_result_count(result))
                return result
        raise ValueError(f"Unknown MCP tool: {name}")
