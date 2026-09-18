from __future__ import annotations

import hashlib

import pytest

from harborrag_core.contracts.tools import BaseTool, ToolInvocationContext, ToolSpec
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.server.api_keys import create_api_key_verifier
from harborrag_mcp_server.server.server import McpServer


@pytest.mark.asyncio
async def test_reader_key_is_tenant_bound_and_revocable(tmp_path, monkeypatch):
    secret = "reader-key-" + "a" * 40
    monkeypatch.setenv("TEST_MCP_KEY_HASH", hashlib.sha256(secret.encode()).hexdigest())
    path = tmp_path / "keys.yaml"
    path.write_text(
        "version: 1\nkeys:\n"
        "  - key_id: engineering\n"
        "    principal_id: mcp-reader:engineering\n"
        "    tenant_id: DEFAULT\n"
        "    secret_hash_env: TEST_MCP_KEY_HASH\n",
        encoding="utf-8",
    )
    verifier = create_api_key_verifier(path)
    assert await verifier.verify_token("wrong" * 10) is None
    token = await verifier.verify_token(secret)
    assert token is not None
    assert token.claims["tenants"] == ["DEFAULT"]
    assert token.claims["role"] == "reader"
    assert token.claims["sub"] == "mcp-reader:engineering"
    path.write_text(path.read_text(encoding="utf-8") + "    revoked: true\n", encoding="utf-8")
    assert await verifier.verify_token(secret) is None


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
