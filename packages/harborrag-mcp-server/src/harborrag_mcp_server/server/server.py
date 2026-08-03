from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from harborrag_core.invariants import HarborInvariantError
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.policy import McpToolPolicy
from harborrag_mcp_server.server.base import BaseMcpServer
from harborrag_mcp_server.tools.base import BaseMcpTool, McpToolSpec
from harborrag_mcp_server.tools.chat import ChatTool
from harborrag_mcp_server.tools.graph_search import (
    GraphPathSearchTool,
    GraphSubgraphSearchTool,
    GraphTripletSearchTool,
)
from harborrag_mcp_server.tools.vector_search import (
    AdvancedVectorSearchTool,
    VectorSearchTool,
)

if TYPE_CHECKING:
    from harborrag_mcp_server.configuration import McpConfigurationStore
    from harborrag_runtime.sdk import HarborRAG

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
    for field_name in ("results", "triplets", "paths", "nodes"):
        results = result.get(field_name)
        if isinstance(results, list):
            return len(results)
    return 1


@dataclass(slots=True)
class McpServer(BaseMcpServer):
    """In-process MCP transport enforcing policy and audit boundaries."""

    runtime: HarborRAG | None = None
    tools: list[BaseMcpTool] | None = None
    policy: McpToolPolicy = field(default_factory=lambda: _default_policy)
    audit: McpAuditLog = field(default_factory=lambda: _default_audit_log)
    configuration: McpConfigurationStore | None = None

    def __post_init__(self) -> None:
        if self.tools is None:
            self.tools = [
                VectorSearchTool(runtime=self.runtime),
                AdvancedVectorSearchTool(runtime=self.runtime),
                GraphTripletSearchTool(runtime=self.runtime),
                GraphPathSearchTool(runtime=self.runtime),
                GraphSubgraphSearchTool(runtime=self.runtime),
                ChatTool(runtime=self.runtime),
            ]

    def list_tools(self, tenant_id: str | None = None) -> list[McpToolSpec]:
        if self.tools is None:
            raise HarborInvariantError("self.tools must not be None here")
        if self.configuration is None:
            return [tool.spec for tool in self.tools]
        return [
            self.configuration.tool_spec(tool.spec, tenant_id)
            for tool in self.tools
            if self.configuration.resolve(tool.spec.name, tenant_id).enabled
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        principal_id: str = "in-process",
    ) -> dict[str, object]:
        payload = dict(arguments or {})
        invocation_id = self.audit.start(name, payload, principal_id=principal_id)
        try:
            if self.tools is None:
                raise HarborInvariantError("self.tools must not be None here")
            for tool in self.tools:
                if tool.spec.name != name:
                    continue
                policy = self.policy
                spec = tool.spec
                if self.configuration is not None:
                    tenant_value = payload.get("tenant_id")
                    tenant_id = tenant_value if isinstance(tenant_value, str) else None
                    configured = self.configuration.resolve(name, tenant_id)
                    if not configured.enabled:
                        raise PermissionError(f"MCP tool {name} is disabled")
                    payload = {**configured.defaults, **payload}
                    spec = self.configuration.tool_spec(spec, tenant_id)
                    policy = self.configuration.policy()
                policy.check_call(spec, payload)
                result = await tool.call(payload, principal_id=principal_id)
                policy.check_results(_result_count(result))
                policy.check_output(result)
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
