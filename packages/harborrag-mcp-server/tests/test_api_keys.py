from __future__ import annotations

import pytest

from harborrag_core.contracts.tools import BaseTool, ToolInvocationContext, ToolSpec
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.server.server import McpServer


class ModeProbe(BaseTool):
    spec = ToolSpec(
        name="mode_probe",
        description="Inspect the server-bound policy in a test.",
        input_schema={
            "type": "object",
            "properties": {"tenant_id": {"type": "string"}},
            "required": ["tenant_id"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"mode": {"type": "string"}}},
    )

    async def call(self, arguments, *, principal_id, context: ToolInvocationContext | None = None):
        assert context is not None
        return {"mode": context.access.corpus_mode}


@pytest.mark.asyncio
async def test_shared_mode_is_only_applied_to_its_configured_tenant() -> None:
    server = McpServer(
        tools=[ModeProbe()],
        audit=McpAuditLog(),
        corpus_mode="tenant_shared",
        shared_tenant_id="DEFAULT",
    )
    assert (await server.call_tool("mode_probe", {"tenant_id": "DEFAULT"}))["mode"] == (
        "tenant_shared"
    )
    assert (await server.call_tool("mode_probe", {"tenant_id": "RESTRICTED"}))["mode"] == (
        "source_acl"
    )
