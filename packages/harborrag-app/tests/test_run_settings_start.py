"""The app's start and recovery paths pin run settings once and never re-resolve them."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from harborrag_app.workflow_control.ingestion.connection_sources import ConnectionSourceInputBuilder
from harborrag_app.workflow_control.ingestion.recovery import source_from_task
from harborrag_core.config_store import SettingsLevel, SettingsSnapshot, resolve_effective
from harborrag_core.domain.connection import ConnectionBuilder
from harborrag_core.ingestion import IngestionTask, IngestionTaskState
from harborrag_core.testing.control_plane_fakes import (
    FakeConnectionRepository,
    FakeConnectionRevisionRepository,
)
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config_store.defaults import registry_for
from harborrag_runtime.ingestion_contracts import RunSettings, SourceSubmission

pytestmark = pytest.mark.unit


class _Provider:
    def __init__(self) -> None:
        self.effective = AsyncMock(side_effect=self._effective)

    async def _effective(self, tenant_id: str, level: SettingsLevel):  # noqa: ANN202
        snapshot = SettingsSnapshot(
            tenant_id, 4, {"connection:default/docs": {"chunking.max_tokens": 256}}
        )
        return resolve_effective(registry_for(RuntimeSettings()), snapshot, level)


async def _builder(provider: _Provider | None) -> ConnectionSourceInputBuilder:
    connections = FakeConnectionRepository()
    await connections.put(
        ConnectionBuilder.for_new(tenant_id="tenant-1", project_id="default", connection_id="docs")
        .source_type("local")
        .source_key("docs")
        .config({"source_path": "/data/sources"})
        .build()
    )
    plane: Any = SimpleNamespace(
        connections=connections,
        connection_revisions=FakeConnectionRevisionRepository(),
        config=provider,
    )
    return ConnectionSourceInputBuilder(lambda: plane, prepare=lambda *args, **kwargs: None)  # type: ignore[arg-type,return-value]


_SUBMISSION = SourceSubmission(
    task_id="run-1", tenant_id="tenant-1", connector_name="docs", connection_id="docs"
)


@pytest.mark.asyncio
async def test_start_resolves_the_connection_run_settings() -> None:
    resolved = await (await _builder(_Provider())).resolve(_SUBMISSION)

    assert resolved.run_settings == RunSettings("rev_4", None, 256, None)


@pytest.mark.asyncio
async def test_start_without_a_config_provider_pins_nothing() -> None:
    resolved = await (await _builder(None)).resolve(_SUBMISSION)

    assert resolved.run_settings is None


@pytest.mark.asyncio
async def test_recovery_restores_pinned_settings() -> None:
    provider = _Provider()
    pinned = RunSettings("rev_4", "docling", 256, 2)
    task = IngestionTask(
        task_id="run-1",
        source_scope_id="scope",
        status=IngestionTaskState.FAILED,
        request={
            "tenant_id": "tenant-1",
            "connector_name": "docs",
            "connector_type": "local",
            "connection_id": "docs",
            "source_scope_id": "scope",
            "configuration_fingerprint": "fp",
            "settings_revision": "rev_4",
            "run_settings": pinned.to_json(),
            "processing": {
                "parser_profile": "p",
                "normalizer_version": "n",
                "chunk_strategy": "c",
                "dense_encoder_profile": "d",
                "sparse_encoder_profile": "s",
                "graph_projection_version": "g",
            },
            "query": {},
        },
    )

    prepared = source_from_task(task)

    provider.effective.assert_not_called()
    assert prepared.run_settings == pinned
    assert (
        source_from_task(
            task.model_copy(update={"request": {**task.request, "run_settings": None}})
        ).run_settings
        is None
    )
