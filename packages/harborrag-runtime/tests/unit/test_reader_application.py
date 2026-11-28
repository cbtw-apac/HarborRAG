"""The MCP reader composition owns only reader resources."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_core.contracts.tools import ToolInvocationContext
from harborrag_core.security import AccessContext
from harborrag_mcp_server.server import create_mcp_server
from harborrag_runtime.composition.readers import ReaderApplication, open_reader_application
from harborrag_runtime.config.settings import RuntimeSettings

pytestmark = pytest.mark.unit

_POSTGRES_URL = "postgresql+asyncpg://reader:example@127.0.0.1:5432/harbor"
_GRANT_CHECK = (
    "harborrag_adapters.repositories.database.control_plane.reader_grants."
    "tables_beyond_reader_grants"
)
_CONNECT_RETRIEVAL = "harborrag_runtime.retrieval.composition.connect_retrieval_service"


def test_reader_import_does_not_load_optional_layers() -> None:
    script = """
import sys
from harborrag_runtime.composition.readers import open_reader_application
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_mcp_server.server import McpServer
application = open_reader_application(RuntimeSettings())
assert len(McpServer(tools=application.tools).list_tools()) == 11
assert all(getattr(tool, 'runtime', None) is None for tool in application.tools)
for prefix in ('harborrag_memory', 'harborrag_runtime.chat',
               'harborrag_runtime.sdk',
               'harborrag_runtime.ingestion'):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_reader_application_closes_its_service_once(monkeypatch: pytest.MonkeyPatch) -> None:
    service = type("Service", (), {"aclose": AsyncMock()})()
    connect = AsyncMock(return_value=service)
    monkeypatch.setattr(_CONNECT_RETRIEVAL, connect)
    application = open_reader_application(RuntimeSettings())

    await application.start()
    await application.start()
    await application.aclose()

    connect.assert_awaited_once()
    service.aclose.assert_awaited_once()


def _postgres_application(
    monkeypatch: pytest.MonkeyPatch, *, beyond: tuple[str, ...]
) -> tuple[ReaderApplication, AsyncMock, AsyncMock]:
    """A Postgres-backed reader whose settings store, grants and retrieval are stand-ins."""
    check = AsyncMock(return_value=beyond)
    connect = AsyncMock(return_value=type("Service", (), {"aclose": AsyncMock()})())
    monkeypatch.setattr(_GRANT_CHECK, check)
    monkeypatch.setattr(_CONNECT_RETRIEVAL, connect)
    application = open_reader_application(RuntimeSettings(control_db_url=_POSTGRES_URL))
    assert application.config_provider is not None
    monkeypatch.setattr(application.config_provider, "start", AsyncMock())
    return application, check, connect


@pytest.mark.asyncio
async def test_reader_application_refuses_to_start_when_the_mcp_login_reads_other_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _, connect = _postgres_application(
        monkeypatch, beyond=("ingestion_tasks", "task_events")
    )

    try:
        with pytest.raises(HarborConfigurationError) as refused:
            await application.start()
    finally:
        await application.aclose()

    message = str(refused.value)
    assert message.startswith("Postgres login harborrag_mcp can read 2 tables outside")
    assert "apply the control-plane migrations" in message
    assert message.endswith(": ingestion_tasks, task_events")
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_reader_application_starts_when_the_mcp_login_reads_only_its_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, check, connect = _postgres_application(monkeypatch, beyond=())

    try:
        await application.start()
    finally:
        await application.aclose()

    check.assert_awaited_once_with(application._config_engine)
    connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_reader_application_checks_no_grants_over_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = AsyncMock(return_value=("ingestion_tasks",))
    monkeypatch.setattr(_GRANT_CHECK, check)
    monkeypatch.setattr(_CONNECT_RETRIEVAL, AsyncMock())
    application = open_reader_application(RuntimeSettings())

    await application.start()
    await application.aclose()

    check.assert_not_awaited()


@pytest.mark.asyncio
async def test_reader_invocation_uses_shared_execution_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audit_path = tmp_path / "execution.jsonl"
    monkeypatch.setenv("HARBORRAG_TOOL_EXECUTION_AUDIT_PATH", str(audit_path))
    application = open_reader_application(RuntimeSettings())
    context = ToolInvocationContext(AccessContext(principal_id="reader", tenant_id="acme"))

    result = await application.invoker.invoke("describe_graph", {}, context=context)

    assert result["ok"] is True
    assert "tool_execution_completed" in audit_path.read_text()
    assert application._service is None


@pytest.mark.asyncio
async def test_reader_application_offers_a_tenant_directory_only_over_postgres() -> None:
    over_postgres = open_reader_application(RuntimeSettings(control_db_url=_POSTGRES_URL))
    over_sqlite = open_reader_application(RuntimeSettings())
    try:
        assert over_postgres.tenant_directory is not None
        assert over_sqlite.tenant_directory is None
    finally:
        await over_postgres.aclose()
        await over_sqlite.aclose()


def test_mcp_factory_uses_reader_application_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    application = open_reader_application(RuntimeSettings())

    def unexpected_catalog(*args: object, **kwargs: object) -> None:
        raise AssertionError("MCP must use the reader application's bound catalog")

    monkeypatch.setattr(
        "harborrag_mcp_server.server.server.build_reader_tool_catalog", unexpected_catalog
    )
    assert create_mcp_server(runtime=application, allow_unauthenticated_local=True)
