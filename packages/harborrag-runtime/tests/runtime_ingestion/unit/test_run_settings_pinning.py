"""A run pins its ``next_ingestion`` settings and revision once, when it is prepared."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from temporalio.converter import DataConverter

from harborrag_core.config_store import SettingsLevel, SettingsSnapshot, resolve_effective
from harborrag_core.ingestion import IngestionTask, IngestionTaskState
from harborrag_core.testing.control_plane_fakes import (
    FakeConnectionRepository,
    FakeConnectionRevisionRepository,
)
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config_store.defaults import registry_for
from harborrag_runtime.config_store.run_settings import RunSettingsResolver
from harborrag_runtime.execution.gateway import prepare_configured_source_submission
from harborrag_runtime.execution.task_registry import IngestionTaskRegistry
from harborrag_runtime.ingestion.connections import ConnectionResolver
from harborrag_runtime.ingestion.profiles import build_processing_profile
from harborrag_runtime.ingestion_contracts import RunSettings, SourceSubmission
from harborrag_runtime.temporal.schemas import (
    ProcessingProfileInput,
    SourceIngestionInput,
    SourceQuery,
)
from runtime_ingestion.connection_support import connection_row

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[5]
CONNECTION_LEVEL = "connection:default/docs"


class _MutableProvider:
    """Config provider over a snapshot the test can replace between runs."""

    def __init__(self, snapshot: SettingsSnapshot) -> None:
        self.snapshot_value = snapshot
        self.effective_calls = 0

    async def snapshot(self, tenant_id: str) -> SettingsSnapshot:
        return self.snapshot_value

    async def effective(self, tenant_id: str, level: SettingsLevel):  # noqa: ANN201
        self.effective_calls += 1
        return resolve_effective(registry_for(RuntimeSettings()), self.snapshot_value, level)

    def invalidate(self, tenant_id: str | None = None) -> None:
        return None


@pytest.fixture
def shipped_settings() -> RuntimeSettings:
    return RuntimeSettings(
        parser_config_path=REPO_ROOT / "config/parsers.yaml",
        chunking_config_path=REPO_ROOT / "config/chunking.yaml",
        temporal_config_path=REPO_ROOT / "config/temporal.yaml",
    )


async def _stored_connection() -> tuple[FakeConnectionRepository, FakeConnectionRevisionRepository]:
    connections = FakeConnectionRepository()
    await connections.put(connection_row("local", config={"source_path": "/data/sources"}))
    return connections, FakeConnectionRevisionRepository()


def _snapshot(revision: int, overrides: dict[str, object]) -> SettingsSnapshot:
    return SettingsSnapshot("tenant-1", revision, {CONNECTION_LEVEL: overrides})


async def _resolve(provider: _MutableProvider | None = None, **kwargs: object):  # noqa: ANN202
    connections, revisions = await _stored_connection()
    resolver = ConnectionResolver(
        connections,
        revisions,
        run_settings=RunSettingsResolver(provider) if provider is not None else None,
    )
    return await resolver.resolve(tenant_id="tenant-1", source_key="docs")


def _submission(**kwargs: object) -> SourceSubmission:
    return SourceSubmission(
        task_id="run-1",
        tenant_id="tenant-1",
        connector_name="docs",
        connection_id="docs",
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_resolver_attaches_run_settings() -> None:
    provider = _MutableProvider(_snapshot(4, {"chunking.max_tokens": 256}))

    resolved = await _resolve(provider)

    assert resolved.run_settings == RunSettings("rev_4", None, 256, None)
    assert (await _resolve()).run_settings is None


@pytest.mark.asyncio
async def test_prepared_submission_carries_run_settings_and_identity(
    shipped_settings: RuntimeSettings,
) -> None:
    resolved = await _resolve(_MutableProvider(_snapshot(4, {"chunking.max_tokens": 256})))

    prepared = prepare_configured_source_submission(
        shipped_settings, _submission(), connection=resolved
    )

    assert prepared.run_settings is not None
    assert prepared.run_settings.chunk_max_tokens == 256
    assert (
        prepared.processing.chunk_strategy
        != build_processing_profile(shipped_settings).chunk_strategy
    )


@pytest.mark.asyncio
async def test_concurrency_precedence(shipped_settings: RuntimeSettings) -> None:
    plain = await _resolve()
    overridden = await _resolve(_MutableProvider(_snapshot(1, {"ingestion.concurrency": 16})))

    def concurrency(connection, **submission: object) -> int:  # noqa: ANN001
        return prepare_configured_source_submission(
            shipped_settings, _submission(**submission), connection=connection
        ).document_concurrency

    assert concurrency(overridden, document_concurrency=3) == 3
    assert concurrency(overridden) == 16
    assert concurrency(plain) == 8


def _source(run_settings: RunSettings | None) -> SourceIngestionInput:
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
        query=SourceQuery(),
        run_settings=run_settings,
    )


def _registry() -> tuple[IngestionTaskRegistry, AsyncMock]:
    register = AsyncMock()
    control = SimpleNamespace(
        source_scans=SimpleNamespace(register_scope=AsyncMock()),
        tasks=SimpleNamespace(register=register),
    )
    return IngestionTaskRegistry(control), register  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_task_request_records_settings_revision() -> None:
    registry, register = _registry()
    pinned = RunSettings("rev_4", None, 256, None)

    await registry.register(_source(pinned), actor="alex@example.com")

    request = register.await_args.args[0].request
    assert request["settings_revision"] == "rev_4"
    assert request["run_settings"] == pinned.to_json()
    assert request["actor"] == "alex@example.com"


@pytest.mark.asyncio
async def test_task_request_without_run_settings_carries_neither() -> None:
    registry, register = _registry()

    await registry.register(_source(None))

    request = register.await_args.args[0].request
    assert "settings_revision" not in request
    assert "run_settings" not in request


@pytest.mark.asyncio
async def test_retry_run_copies_settings_revision() -> None:
    registry, register = _registry()
    pinned = RunSettings("rev_4", "docling", 256, 2)
    await registry.register(_source(pinned))
    original = IngestionTask(
        task_id="task-1",
        source_scope_id="engineering-docs",
        status=IngestionTaskState.FAILED,
        request=register.await_args.args[0].request,
    )

    await registry.register_retry(
        retry_task_id="retry-1", original=original, document_ids=["doc-1"], actor="alex@example.com"
    )

    request = register.await_args.args[0].request
    assert request["settings_revision"] == "rev_4"
    assert request["run_settings"] == pinned.to_json()
    assert request["actor"] == "alex@example.com"


@pytest.mark.asyncio
async def test_scheduled_preparation_pins_settings(
    shipped_settings: RuntimeSettings,
) -> None:
    from harborrag_runtime.config.temporal import TemporalRuntimeConfig
    from harborrag_runtime.temporal.ingestion_activities import IngestionActivities
    from harborrag_runtime.temporal.schemas import ScheduledSourcePreparationInput

    connections, revisions = await _stored_connection()
    provider = _MutableProvider(_snapshot(4, {"chunking.max_tokens": 256}))
    runtime = SimpleNamespace(
        control=SimpleNamespace(
            source_scans=SimpleNamespace(has_running_scan=AsyncMock(return_value=False))
        ),
        source_plans=None,
        connection_resolver=ConnectionResolver(
            connections, revisions, run_settings=RunSettingsResolver(provider)
        ),
    )
    activities = IngestionActivities(
        runtime,  # type: ignore[arg-type]
        settings=shipped_settings,
        temporal_config=TemporalRuntimeConfig(),
    )

    preparation = await activities.prepare_scheduled_source(
        ScheduledSourcePreparationInput(
            task_id="scheduled-1", tenant_id="tenant-1", connection_id="docs"
        )
    )

    assert preparation.source is not None
    assert preparation.source.run_settings == RunSettings("rev_4", None, 256, None)


def test_source_input_without_run_settings_still_loads() -> None:
    converter = DataConverter.default.payload_converter
    (payload,) = converter.to_payloads([_source(RunSettings("rev_4", None, 256, None))])
    stored = json.loads(payload.data)
    del stored["run_settings"]

    (decoded,) = converter.from_payloads(converter.to_payloads([stored]), [SourceIngestionInput])

    assert decoded.run_settings is None
    (round_trip,) = converter.from_payloads(
        converter.to_payloads([_source(RunSettings("rev_4", None, 256, None))]),
        [SourceIngestionInput],
    )
    assert round_trip.run_settings == RunSettings("rev_4", None, 256, None)


@pytest.mark.asyncio
async def test_patch_after_start_keeps_the_running_run(shipped_settings: RuntimeSettings) -> None:
    provider = _MutableProvider(_snapshot(3, {"chunking.max_tokens": 512}))
    connections, revisions = await _stored_connection()
    resolver = ConnectionResolver(
        connections, revisions, run_settings=RunSettingsResolver(provider)
    )

    async def prepare():  # noqa: ANN202
        connection = await resolver.resolve(tenant_id="tenant-1", source_key="docs")
        return prepare_configured_source_submission(
            shipped_settings, _submission(), connection=connection
        )

    first = await prepare()
    provider.snapshot_value = _snapshot(4, {"chunking.max_tokens": 256})
    next_run = await prepare()

    assert first.run_settings == RunSettings("rev_3", None, 512, None)
    assert next_run.run_settings == RunSettings("rev_4", None, 256, None)
    assert first.processing != next_run.processing


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"chunking.max_tokens": 256}, RunSettings("rev_4", None, 256, None)),
        ({}, RunSettings("rev_4")),
    ],
)
async def test_sdk_temporal_input_pins_run_settings(
    monkeypatch: pytest.MonkeyPatch,
    shipped_settings: RuntimeSettings,
    overrides: dict[str, object],
    expected: RunSettings,
) -> None:
    from harborrag_runtime.contracts import AccessContext, IngestionRequest
    from harborrag_runtime.execution import temporal as temporal_module

    connections, revisions = await _stored_connection()
    config = _MutableProvider(_snapshot(4, overrides))
    config.start = AsyncMock()  # type: ignore[attr-defined]
    config.aclose = AsyncMock()  # type: ignore[attr-defined]
    stores = SimpleNamespace(
        connections=connections, revisions=revisions, engine=object(), config_store=object()
    )
    monkeypatch.setattr(temporal_module, "build_connection_stores", lambda _settings: stores)
    monkeypatch.setattr(temporal_module, "build_config_provider", lambda *_a, **_k: config)
    executor = temporal_module.TemporalIngestionExecutor(shipped_settings)
    request = IngestionRequest(
        access=AccessContext(principal_id="user-1", tenant_id="tenant-1"),
        connector_name="docs",
        task_id="run-1",
        connection_id="docs",
    )

    source = await executor._input(request)  # noqa: SLF001 - the input the SDK path starts runs with

    assert source.run_settings == expected
    config.start.assert_awaited_once_with()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_concurrent_first_submits_start_one_config_provider(
    monkeypatch: pytest.MonkeyPatch, shipped_settings: RuntimeSettings
) -> None:
    from harborrag_runtime.contracts import AccessContext, IngestionRequest
    from harborrag_runtime.execution import temporal as temporal_module

    connections, revisions = await _stored_connection()
    config = _MutableProvider(_snapshot(4, {}))

    async def yielding_start() -> None:
        await asyncio.sleep(0)

    config.start = AsyncMock(side_effect=yielding_start)  # type: ignore[attr-defined]
    config.aclose = AsyncMock()  # type: ignore[attr-defined]
    build = Mock(return_value=config)
    stores = SimpleNamespace(
        connections=connections, revisions=revisions, engine=object(), config_store=object()
    )
    monkeypatch.setattr(temporal_module, "build_connection_stores", lambda _settings: stores)
    monkeypatch.setattr(temporal_module, "build_config_provider", build)
    executor = temporal_module.TemporalIngestionExecutor(shipped_settings)
    request = IngestionRequest(
        access=AccessContext(principal_id="user-1", tenant_id="tenant-1"),
        connector_name="docs",
        task_id="run-1",
        connection_id="docs",
    )

    await asyncio.gather(*(executor._input(request) for _ in range(3)))  # noqa: SLF001

    build.assert_called_once()
    config.start.assert_awaited_once_with()  # type: ignore[attr-defined]
