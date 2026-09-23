"""The MCP reader composition owns only reader resources."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from harborrag_core.contracts.tools import ToolInvocationContext
from harborrag_core.security import AccessContext
from harborrag_mcp_server.server import create_mcp_server
from harborrag_runtime.composition.readers import open_reader_application
from harborrag_runtime.config.settings import RuntimeSettings


def test_reader_import_does_not_load_optional_layers() -> None:
    script = """
import sys
from harborrag_runtime.composition.readers import open_reader_application
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_mcp_server.server import McpServer
application = open_reader_application(RuntimeSettings())
assert len(McpServer(tools=application.tools).list_tools()) == 13
assert all(getattr(tool, 'runtime', None) is None for tool in application.tools)
for prefix in ('harborrag_memory', 'harborrag_runtime.chat',
               'harborrag_runtime.agent', 'harborrag_runtime.sdk',
               'harborrag_runtime.ingestion'):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_reader_application_closes_its_service_once(monkeypatch: pytest.MonkeyPatch) -> None:
    service = type("Service", (), {"aclose": AsyncMock()})()
    connect = AsyncMock(return_value=service)
    monkeypatch.setattr(
        "harborrag_runtime.retrieval.composition.connect_retrieval_service", connect
    )
    application = open_reader_application(RuntimeSettings())

    await application.start()
    await application.start()
    await application.aclose()

    connect.assert_awaited_once()
    service.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_reader_invocation_uses_shared_execution_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    audit_path = tmp_path / "execution.jsonl"
    monkeypatch.setenv("HARBORRAG_TOOL_EXECUTION_AUDIT_PATH", str(audit_path))
    application = open_reader_application(RuntimeSettings())
    context = ToolInvocationContext(AccessContext(principal_id="reader", tenant_id="acme"))

    result = await application.invoker.invoke("describe_graph", {}, context=context)

    assert result["ok"] is True
    assert "tool_execution_completed" in audit_path.read_text()
    assert application._service is None


def test_mcp_factory_uses_reader_application_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    application = open_reader_application(RuntimeSettings())

    def unexpected_catalog(*args: object, **kwargs: object) -> None:
        raise AssertionError("MCP must use the reader application's bound catalog")

    monkeypatch.setattr(
        "harborrag_mcp_server.server.server.build_reader_tool_catalog", unexpected_catalog
    )
    assert create_mcp_server(runtime=application, allow_unauthenticated_local=True)
