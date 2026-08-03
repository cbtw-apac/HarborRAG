from __future__ import annotations

from dataclasses import dataclass

import pytest

from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.configuration import (
    ConfigurationRevisionError,
    McpConfiguration,
    McpConfigurationStore,
)
from harborrag_mcp_server.server.server import McpServer
from harborrag_mcp_server.tools.base import BaseMcpTool, McpToolSpec


@dataclass(slots=True)
class ConfigurableTool(BaseMcpTool):
    spec = McpToolSpec(
        "search",
        "Configurable search.",
        {
            "type": "object",
            "required": ["query", "tenant_id"],
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "tenant_id": {"type": "string", "minLength": 1},
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5,
                },
            },
            "additionalProperties": False,
        },
    )

    async def call(self, arguments, *, principal_id):
        return {"results": [], "arguments": arguments, "principal_id": principal_id}


def _store(tmp_path, configuration, *, environment=None):
    audit = McpAuditLog(path=tmp_path / "audit.jsonl")
    return McpConfigurationStore(
        path=tmp_path / "mcp.yaml",
        configuration=McpConfiguration.model_validate(configuration),
        specs=[ConfigurableTool.spec],
        audit=audit,
        environment=environment or {},
    )


@pytest.mark.asyncio
async def test_global_and_tenant_defaults_apply_at_call_time(tmp_path) -> None:
    store = _store(
        tmp_path,
        {
            "tools": {"search": {"defaults": {"top_k": 5}, "limits": {"top_k": 10}}},
            "tenants": {
                "priority": {"tools": {"search": {"defaults": {"top_k": 2}}}},
                "blocked": {"tools": {"search": {"enabled": False}}},
            },
        },
    )
    server = McpServer(tools=[ConfigurableTool()], configuration=store)

    standard = await server.call_tool(
        "search", {"query": "q", "tenant_id": "standard"}, principal_id="owner"
    )
    priority = await server.call_tool(
        "search", {"query": "q", "tenant_id": "priority"}, principal_id="owner"
    )

    assert standard["arguments"]["top_k"] == 5
    assert priority["arguments"]["top_k"] == 2
    with pytest.raises(ValueError, match="tool schema"):
        await server.call_tool(
            "search",
            {"query": "q", "tenant_id": "standard", "top_k": 11},
        )
    with pytest.raises(PermissionError, match="disabled"):
        await server.call_tool("search", {"query": "q", "tenant_id": "blocked"})


def test_configuration_rejects_unsafe_or_unknown_values(tmp_path) -> None:
    with pytest.raises(ValueError, match="Unknown MCP tools"):
        _store(tmp_path, {"tools": {"missing": {"enabled": True}}})
    with pytest.raises(ValueError, match="must not default required"):
        _store(tmp_path, {"tools": {"search": {"defaults": {"tenant_id": "unsafe"}}}})
    with pytest.raises(ValueError, match="safety maximum"):
        _store(tmp_path, {"tools": {"search": {"limits": {"top_k": 21}}}})
    with pytest.raises(ValueError, match="Invalid default"):
        _store(tmp_path, {"tools": {"search": {"defaults": {"top_k": "many"}}}})


def test_environment_overrides_are_effective_but_not_persisted(tmp_path) -> None:
    store = _store(
        tmp_path,
        {"tools": {"search": {"enabled": True}}},
        environment={
            "HARBORRAG_MCP_DISABLED_TOOLS": "search",
            "HARBORRAG_MCP_MAX_RESULTS": "7",
        },
    )

    description = store.describe()

    assert store.enabled_tool_names() == []
    assert store.policy().max_results == 7
    assert description["configuration"]["tools"]["search"]["enabled"] is True
    assert description["effective"]["tools"]["search"]["enabled"] is False
    assert description["environment_overrides"] == [
        "HARBORRAG_MCP_DISABLED_TOOLS",
        "HARBORRAG_MCP_MAX_RESULTS",
    ]


def test_atomic_replace_revision_reload_and_audit(tmp_path) -> None:
    store = _store(tmp_path, {"tools": {"search": {"enabled": True}}})
    initial = store.describe()
    replacement = McpConfiguration.model_validate({"tools": {"search": {"enabled": False}}})

    saved = store.replace(
        replacement,
        principal_id="owner-1",
        expected_revision=initial["revision"],
    )

    assert saved["restart_required"] is True
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.audit.entries[-1]["event"] == "configuration_changed"
    assert "configuration" not in store.audit.entries[-1]
    with pytest.raises(ConfigurationRevisionError):
        store.replace(
            replacement,
            principal_id="owner-1",
            expected_revision=initial["revision"],
        )
    reloaded = store.reload(principal_id="owner-2")
    assert reloaded["configuration"]["tools"]["search"]["enabled"] is False
