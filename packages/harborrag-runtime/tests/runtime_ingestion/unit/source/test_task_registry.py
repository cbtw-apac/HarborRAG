"""Source task registration behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harborrag_core.ingestion import IngestionTaskState
from harborrag_runtime.temporal.schemas import (
    ProcessingProfileInput,
    SourceIngestionInput,
    SourceQuery,
)
from harborrag_runtime.temporal.task_registry import (
    IngestionTaskRegistry,
)


def _source() -> SourceIngestionInput:
    return SourceIngestionInput(
        task_id="task-1",
        tenant_id="tenant-1",
        connector_name="local-docs",
        connector_type="local",
        connection_id="local-docs",
        source_scope_id="engineering-docs",
        configuration_fingerprint="config-v1",
        processing=ProcessingProfileInput(
            parser_profile="parser-v1",
            normalizer_version="normalizer-v1",
            chunk_strategy="chunks-v1",
            dense_encoder_profile="dense-v1",
            sparse_encoder_profile="sparse-v1",
            graph_projection_version="graph-v1",
        ),
        query=SourceQuery(
            path="guides",
            pattern="*.md",
            limit=5,
            filters_json='{"labels":["release"]}',
        ),
    )


@pytest.mark.asyncio
async def test_registry_persists_pending_task_before_temporal_submission() -> None:
    source_scans = SimpleNamespace(register_scope=AsyncMock())
    tasks = SimpleNamespace(register=AsyncMock())
    control = SimpleNamespace(
        source_scans=source_scans,
        tasks=tasks,
        close=AsyncMock(),
    )
    registry = IngestionTaskRegistry(control)

    await registry.register(_source())

    source_scans.register_scope.assert_awaited_once_with(
        tenant_id="tenant-1",
        source_scope_id="engineering-docs",
        connector_type="local",
        connection_id="local-docs",
        configuration_fingerprint="config-v1",
    )
    task = tasks.register.await_args.args[0]
    assert task.status == IngestionTaskState.PENDING
    assert task.request["tenant_id"] == "tenant-1"
    assert task.request["query"]["path"] == "guides"
    assert task.request["query"]["filters"] == {"labels": ["release"]}
    assert task.request["processing"]["dense_encoder_profile"] == "dense-v1"
    assert tasks.register.await_args.kwargs == {
        "idempotency_key": None,
        "request_hash": None,
    }

    await registry.close()
    control.close.assert_awaited_once()
