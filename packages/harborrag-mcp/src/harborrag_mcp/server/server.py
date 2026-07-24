from __future__ import annotations

from dataclasses import dataclass, field

from harborrag_mcp.audit import McpAuditLog
from harborrag_mcp.policy import McpToolPolicy
from harborrag_mcp.server.base import BaseMcpServer
from harborrag_mcp.tools.base import BaseMcpTool, McpToolSpec
from harborrag_mcp.tools.health import HealthTool

# Shared, process-wide default policy/audit singletons. The module-level
# call_tool/list_tools facade constructs a fresh McpServer per invocation, so
# these live outside the dataclass defaults to retain one audit trail and
# policy across facade calls.
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
class McpServer(BaseMcpServer):
    """In-process MCP transport enforcing policy and audit boundaries."""

    tools: list[BaseMcpTool] = field(default_factory=lambda: [HealthTool()])
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
