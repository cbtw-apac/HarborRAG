"""MCP and agent use the same checked reader invocation."""

from dataclasses import dataclass

import pytest

from harborrag_core.contracts.tools import BaseTool, ToolInvocationContext, ToolSpec
from harborrag_engine.tools.dispatcher import ToolInvoker
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.server import McpServer
from harborrag_runtime.composition.agent_tools import RuntimeAgentToolProvider


@dataclass
class ReaderTool(BaseTool):
    spec = ToolSpec(
        "reader_probe",
        "Read one tenant-scoped probe.",
        {
            "type": "object",
            "required": ["tenant_id"],
            "properties": {"tenant_id": {"type": "string"}},
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "required": ["ok", "principal"],
            "properties": {"ok": {"const": True}, "principal": {"type": "string"}},
            "additionalProperties": False,
        },
    )

    async def call(self, arguments: dict[str, object], *, principal_id: str) -> dict[str, object]:
        return {"ok": True, "principal": principal_id}


class DenyBlockedTenant:
    def allowed(self, context: ToolInvocationContext, name: str) -> bool:
        return str(context.access.tenant_id) != "blocked"


@pytest.mark.asyncio
async def test_same_result_and_tenant_restriction_through_mcp_and_agent() -> None:
    invoker = ToolInvoker([ReaderTool()], access_policy=DenyBlockedTenant())
    mcp = McpServer(invoker=invoker, audit=McpAuditLog())
    agent = RuntimeAgentToolProvider(reader_invoker=invoker)

    arguments = {"tenant_id": "allowed"}
    assert await mcp.call_tool("reader_probe", arguments, principal_id="reader") == (
        await agent.call_tool("reader_probe", arguments, principal_id="reader")
    )
    with pytest.raises(PermissionError, match="disabled"):
        await mcp.call_tool("reader_probe", {"tenant_id": "blocked"}, principal_id="reader")
    assert await agent.call_tool(
        "reader_probe", {"tenant_id": "blocked"}, principal_id="reader"
    ) == {"ok": False, "error": "tool reader_probe is disabled"}


@pytest.mark.asyncio
async def test_agent_rejects_model_supplied_cross_tenant_request() -> None:
    agent = RuntimeAgentToolProvider(
        reader_invoker=ToolInvoker([ReaderTool()]), tenant_id="allowed"
    )

    assert await agent.call_tool("reader_probe", {"tenant_id": "other"}, principal_id="reader") == {
        "ok": False,
        "error": "reader tool tenant does not match authenticated context",
    }
