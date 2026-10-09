"""The legacy ``connector_name`` submission path enforces connection tenant allowlists."""

from __future__ import annotations

from pathlib import Path

import pytest
from workflow_control_fixtures import FakeRuntimeClient, FakeTaskRegistry

from harborrag_app.workflow_control.ingestion.durable import DurableIngestionOperations
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.execution.source_submission import prepare_source_submission

_CATALOG = """
version: 1
connectors:
  acme-docs:
    provider: local
    tenants: [acme]
    settings:
      source_path: docs
"""


def _operations(tmp_path: Path) -> tuple[DurableIngestionOperations, FakeRuntimeClient]:
    connectors = tmp_path / "connectors.yaml"
    connectors.write_text(_CATALOG, encoding="utf-8")
    parsers = tmp_path / "parsers.yaml"
    parsers.write_text("version: 1\n", encoding="utf-8")
    client = FakeRuntimeClient()
    registry = FakeTaskRegistry()

    async def runtime_client() -> FakeRuntimeClient:
        return client

    async def task_registry() -> FakeTaskRegistry:
        return registry

    operations = DurableIngestionOperations(
        RuntimeSettings(connector_config_path=connectors, parser_config_path=parsers),
        runtime_client=runtime_client,  # type: ignore[arg-type]
        task_registry=task_registry,  # type: ignore[arg-type]
        source_input_builder=prepare_source_submission,
    )
    return operations, client


@pytest.mark.asyncio
async def test_legacy_start_ingestion_refuses_a_connection_outside_the_allowlist(
    tmp_path: Path,
) -> None:
    operations, client = _operations(tmp_path)

    refused = await operations.start_ingestion(tenant_id="globex", connector_name="acme-docs")
    allowed = await operations.start_ingestion(tenant_id="acme", connector_name="acme-docs")

    assert refused.ok is False
    assert refused.data["error_type"] == "ConnectorNotFoundError"
    assert allowed.ok is True
    assert [name for name, _ in client.calls] == ["start_ingestion"]
