from __future__ import annotations

from harborrag_mcp_server.tools.base import BaseMcpTool, McpToolSpec


class HealthTool(BaseMcpTool):
    """Report whether the in-process MCP transport is available."""

    spec = McpToolSpec(
        "harborrag_health_check",
        "Return HarborRAG MCP health diagnostics.",
        input_schema={"type": "object", "additionalProperties": False},
    )

    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
    ) -> dict[str, object]:
        del arguments, principal_id
        return {
            "ok": True,
            "diagnostics": {
                "transport": "mcp",
                "ready": True,
            },
        }
