from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread

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
                "filters": {
                    "type": "object",
                    "properties": {
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
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
    server = McpServer(
        tools=[ConfigurableTool()],
        audit=store.audit,
        configuration=store,
    )

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
    with pytest.raises(PermissionError, match="disabled"):
        await server.call_tool("search", {"query": "q", "tenant_id": " blocked "})


def test_configuration_rejects_unsafe_or_unknown_values(tmp_path) -> None:
    with pytest.raises(ValueError, match="Unknown MCP tools"):
        _store(tmp_path, {"tools": {"missing": {"enabled": True}}})
    with pytest.raises(ValueError, match="must not default required"):
        _store(tmp_path, {"tools": {"search": {"defaults": {"tenant_id": "unsafe"}}}})
    with pytest.raises(ValueError, match="safety maximum"):
        _store(tmp_path, {"tools": {"search": {"limits": {"top_k": 21}}}})
    with pytest.raises(ValueError, match="Invalid default"):
        _store(tmp_path, {"tools": {"search": {"defaults": {"top_k": "many"}}}})
    with pytest.raises(ValueError, match="surrounding whitespace"):
        _store(tmp_path, {"tenants": {" demo ": {}}})


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


def test_effective_configuration_does_not_expose_cached_nested_values(tmp_path) -> None:
    store = _store(
        tmp_path,
        {"tools": {"search": {"defaults": {"filters": {"tags": ["original"]}}}}},
    )

    exposed = store.effective()
    exposed.tools["search"].defaults["filters"]["tags"].append("mutated")

    assert store.effective().tools["search"].defaults["filters"] == {"tags": ["original"]}
    assert store.resolve("search", None).defaults["filters"] == {"tags": ["original"]}


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


def test_reload_holds_the_update_lock_while_reading(tmp_path, monkeypatch) -> None:
    import harborrag_mcp_server.configuration.store as store_module

    store = _store(tmp_path, {"tools": {"search": {"enabled": True}}})
    store.replace(
        McpConfiguration.model_validate({"tools": {"search": {"enabled": True}}}),
        principal_id="setup",
    )
    original_read = store_module._read_configuration
    read_started = Event()
    release_read = Event()
    replace_started = Event()
    replace_completed = Event()
    thread_errors: list[BaseException] = []

    def blocked_read(path):
        read_started.set()
        assert release_read.wait(timeout=2)
        return original_read(path)

    monkeypatch.setattr(store_module, "_read_configuration", blocked_read)

    def reload() -> None:
        try:
            store.reload(principal_id="reloader")
        except BaseException as exc:
            thread_errors.append(exc)

    reload_thread = Thread(target=reload)
    reload_thread.start()
    assert read_started.wait(timeout=2)

    replacement = McpConfiguration.model_validate({"tools": {"search": {"enabled": False}}})

    def replace() -> None:
        replace_started.set()
        try:
            store.replace(replacement, principal_id="replacer")
        except BaseException as exc:
            thread_errors.append(exc)
        else:
            replace_completed.set()

    replace_thread = Thread(target=replace)
    replace_thread.start()
    assert replace_started.wait(timeout=2)
    assert not replace_completed.wait(timeout=0.1)
    release_read.set()
    reload_thread.join(timeout=2)
    replace_thread.join(timeout=2)

    assert not reload_thread.is_alive()
    assert not replace_thread.is_alive()
    assert thread_errors == []
    assert replace_completed.is_set()
    assert store.snapshot().tools["search"].enabled is False
