from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.policy import McpToolPolicy
from harborrag_mcp_server.server.base import BaseMcpServer
from harborrag_mcp_server.tools.base import BaseMcpTool, McpToolSpec
from harborrag_mcp_server.tools.health import HealthTool
from harborrag_mcp_server.tools.vector_search import VectorSearchTool

# Shared, process-wide default policy/audit singletons. The module-level
# call_tool/list_tools facade constructs a fresh McpServer per invocation, so
# these live outside the dataclass defaults to retain one audit trail and
# policy across facade calls.
_default_policy = McpToolPolicy()
_default_audit_log = McpAuditLog(
    path=Path(os.environ.get("HARBORRAG_MCP_AUDIT_PATH", ".harborrag/mcp-audit.jsonl"))
)


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

    tools: list[BaseMcpTool] = field(
        default_factory=lambda: [HealthTool(), VectorSearchTool()]
    )
    policy: McpToolPolicy = field(default_factory=lambda: _default_policy)
    audit: McpAuditLog = field(default_factory=lambda: _default_audit_log)

    def list_tools(self) -> list[McpToolSpec]:
        return [tool.spec for tool in self.tools]

    def call_tool(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        principal_id: str = "in-process",
    ) -> dict[str, object]:
        payload = arguments or {}
        invocation_id = self.audit.start(name, payload, principal_id=principal_id)
        try:
            for tool in self.tools:
                if tool.spec.name != name:
                    continue
                self.policy.check_call(tool.spec, payload)
                result = tool.call(payload)
                self.policy.check_results(_result_count(result))
                self.policy.check_output(result)
                self.audit.finish(
                    invocation_id,
                    name,
                    principal_id=principal_id,
                    outcome="success",
                )
                return result
            raise ValueError(f"Unknown MCP tool: {name}")
        except BaseException as exc:
            self.audit.finish(
                invocation_id,
                name,
                principal_id=principal_id,
                outcome="error",
                error_type=type(exc).__name__,
            )
            raise
