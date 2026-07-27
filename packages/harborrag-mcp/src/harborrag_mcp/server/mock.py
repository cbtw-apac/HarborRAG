from __future__ import annotations

from dataclasses import dataclass, field

from harborrag_mcp.audit import McpAuditLog
from harborrag_mcp.policy import McpToolPolicy
from harborrag_mcp.server.base import BaseMcpServer
from harborrag_mcp.tools.base import BaseMcpTool, McpToolSpec
from harborrag_mcp.tools.mock import MockHealthTool, MockRetrieveTool
from harborrag_mcp.tools.vector_search import VectorSearchTool

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


def _apply_pre_call_budget(
    tool: BaseMcpTool,
    arguments: dict[str, object],
    max_results: int,
) -> dict[str, object]:
    """Clamp/validate request size before invoking tools with result-size knobs.

    This prevents oversized vector-search requests from triggering retrieval
    work that is guaranteed to violate policy.max_results.
    """
    payload = dict(arguments)
    if tool.spec.name != "vector_search":
        return payload

    if max_results < 1:
        raise ValueError("MCP result budget exceeded.")

    requested_top_k = payload.get("top_k")
    if requested_top_k is None:
        top_k_schema = (
            tool.spec.input_schema.get("properties", {}).get("top_k", {})
            if isinstance(tool.spec.input_schema, dict)
            else {}
        )
        default_top_k = top_k_schema.get("default")
        try:
            default_top_k_int = int(default_top_k)
        except (TypeError, ValueError):
            default_top_k_int = max_results
        if default_top_k_int > max_results:
            payload["top_k"] = max_results
        return payload

    try:
        requested_top_k_int = int(requested_top_k)
    except (TypeError, ValueError) as exc:
        raise ValueError("vector_search top_k must be an integer") from exc

    if requested_top_k_int < 1:
        raise ValueError("vector_search top_k must be >= 1")
    if requested_top_k_int > max_results:
        payload["top_k"] = max_results
    return payload


@dataclass(slots=True)
class MockMcpServer(BaseMcpServer):
    tools: list[BaseMcpTool] = field(
        default_factory=lambda: [MockHealthTool(), MockRetrieveTool(), VectorSearchTool()]
    )
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
                payload = _apply_pre_call_budget(tool, arguments or {}, self.policy.max_results)
                result = tool.call(payload)
                self.policy.check_results(_result_count(result))
                return result
        raise ValueError(f"Unknown MCP tool: {name}")
