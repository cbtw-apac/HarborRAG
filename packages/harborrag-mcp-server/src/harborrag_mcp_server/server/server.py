from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from harborrag_core.invariants import HarborInvariantError
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.policy import McpToolPolicy
from harborrag_mcp_server.references import KnowledgeReferenceStore
from harborrag_mcp_server.server.base import BaseMcpServer
from harborrag_mcp_server.tools.base import BaseMcpTool, McpToolSpec
from harborrag_mcp_server.tools.catalog_factory import build_reader_tool_catalog
from harborrag_runtime.memory import ConversationRepository, InMemoryConversationMemory

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
    for field_name in (
        "results",
        "items",
        "chunks",
        "sources",
        "candidates",
        "triplets",
        "paths",
        "nodes",
    ):
        results = result.get(field_name)
        if isinstance(results, list):
            return len(results)
    data = result.get("data")
    if isinstance(data, dict):
        counts = [len(value) for value in data.values() if isinstance(value, list)]
        if counts:
            return max(counts)
    return 1


@dataclass(slots=True)
class McpServer(BaseMcpServer):
    """In-process MCP transport enforcing policy and audit boundaries."""

    runtime: HarborRAG | None = None
    memory: ConversationRepository = field(default_factory=InMemoryConversationMemory)
    tools: list[BaseMcpTool] | None = None
    policy: McpToolPolicy = field(default_factory=lambda: _default_policy)
    audit: McpAuditLog = field(default_factory=lambda: _default_audit_log)
    configuration: McpConfigurationStore | None = None
    references: KnowledgeReferenceStore = field(default_factory=KnowledgeReferenceStore)

    def __post_init__(self) -> None:
        if self.tools is None:
            self.tools = build_reader_tool_catalog(self.runtime, self.references)
        missing_output_schema = [
            tool.spec.name for tool in self.tools if tool.spec.output_schema is None
        ]
        if missing_output_schema:
            raise HarborInvariantError(
                "Every registered MCP tool must declare an output_schema; missing for: "
                f"{', '.join(sorted(missing_output_schema))}"
            )

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
        tenant_value = payload.get("tenant_id")
        if isinstance(tenant_value, str):
            # One canonical tenant value must drive configuration, audit, schema
            # validation, and the eventual AccessContext. Otherwise whitespace can
            # select global policy here and a tenant override in the tool layer.
            payload["tenant_id"] = tenant_value.strip()
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
                policy.check_output_schema(result, spec.output_schema)
                policy.check_results(_result_count(result))
                policy.check_output(result)
                reported_error = result.get("ok") is False or result.get("status") == "error"
                self.audit.finish(
                    invocation_id,
                    name,
                    principal_id=principal_id,
                    outcome="error" if reported_error else "success",
                    error_type="ToolReportedError" if reported_error else None,
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
