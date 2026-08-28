"""Security-safe process defaults shared by API package tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_app.workflow_control.ingestion.service import IngestionApplicationService
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.temporal.identity import RuntimeWorkflowRef
from harborrag_runtime.temporal.schemas import (
    ProcessingProfileInput,
    RetryFailuresInput,
    SourceIngestionInput,
)
from harborrag_runtime.temporal.submission import SourceSubmission
from harborrag_runtime.temporal.task_registry import IngestionTaskRegistry


@pytest.fixture(autouse=True)
def _isolated_application_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Use loopback auth and a durable database isolated to each test."""

    monkeypatch.setenv("HARBORRAG_HOST", "127.0.0.1")
    monkeypatch.setenv("HARBORRAG_ENV", "dev")
    monkeypatch.setenv(
        "HARBORRAG_CONTROL_DB_URL",
        f"sqlite+aiosqlite:///{tmp_path}/control.db",
    )


class FakeTemporalClient:
    def __init__(self) -> None:
        self.started: list[SourceIngestionInput] = []
        self.cancelled: list[str] = []
        self.terminated: list[str] = []
        self.retries: list[RetryFailuresInput] = []
        self.execution_state_by_task: dict[str, str] = {}

    async def start_ingestion(self, source: SourceIngestionInput) -> RuntimeWorkflowRef:
        self.started.append(source)
        return RuntimeWorkflowRef(source.task_id, "internal", "internal-run")

    async def cancel(self, task_id: str) -> None:
        self.cancelled.append(task_id)

    async def terminate(self, task_id: str, *, reason: str) -> None:
        self.terminated.append(task_id)
        self.execution_state_by_task[task_id] = "terminated"

    async def start_retry_failures(
        self,
        request: RetryFailuresInput,
    ) -> RuntimeWorkflowRef:
        self.retries.append(request)
        return RuntimeWorkflowRef(request.retry_task_id, "internal", "internal-run")

    async def execution_status(self, task_id: str) -> str:
        return self.execution_state_by_task.get(task_id, "running")


def _source_input(
    _settings: RuntimeSettings,
    submission: SourceSubmission,
) -> SourceIngestionInput:
    return SourceIngestionInput(
        task_id=submission.task_id,
        tenant_id=submission.tenant_id,
        connector_name="harborrag-workspace",
        connector_type="local",
        connection_id="harborrag-workspace",
        source_scope_id="scope-harborrag-workspace",
        configuration_fingerprint="connector-test",
        processing=ProcessingProfileInput(
            parser_profile="parser-v1",
            normalizer_version="normalizer-v1",
            chunk_strategy="chunk-v1",
            dense_encoder_profile="dense-v1",
            sparse_encoder_profile="sparse-v1",
            graph_projection_version="graph-v1",
        ),
    )


@pytest_asyncio.fixture
async def service_resources(
    tmp_path: Path,
) -> AsyncIterator[
    tuple[IngestionApplicationService, IngestionControlPlaneDatabase, FakeTemporalClient]
]:
    client = SQLAlchemyDBClient(
        backend="sqlite",
        url=f"sqlite+aiosqlite:///{tmp_path / 'ingestion-api.db'}",
        pool_size=None,
        max_overflow=None,
        pool_recycle_seconds=1_800,
        echo=False,
    )
    control = IngestionControlPlaneDatabase(client, create_schema=True)
    await control.connect()
    registry = IngestionTaskRegistry(control)
    temporal = FakeTemporalClient()
    identifiers = iter(
        (
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
            "00000000-0000-4000-8000-000000000003",
            "00000000-0000-4000-8000-000000000004",
        )
    )

    async def runtime_client() -> FakeTemporalClient:
        return temporal

    async def task_store() -> IngestionTaskRegistry:
        return registry

    service = IngestionApplicationService(
        RuntimeSettings(ingestion_tenant_id="default"),
        client_provider=runtime_client,
        task_store_provider=task_store,
        source_input_builder=_source_input,
        task_id_factory=lambda: next(identifiers),
    )
    try:
        yield service, control, temporal
    finally:
        await control.close()
