from __future__ import annotations

from harborrag_mcp.tools.base import BaseMcpTool, McpToolSpec


class HealthTool(BaseMcpTool):
    """Report whether the in-process MCP transport is available."""

    spec = McpToolSpec("harborrag_health_check", "Return HarborRAG MCP health diagnostics.")

    def call(self, arguments: dict[str, object]) -> dict[str, object]:
        del arguments
        return {
            "ok": True,
            "diagnostics": {
                "transport": "mcp",
                "ready": True,
            },
        }
