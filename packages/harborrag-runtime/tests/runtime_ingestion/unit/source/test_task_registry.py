"""Source task registration behavior."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.converter import DataConverter

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.ingestion import IngestionTaskState
from harborrag_runtime.execution.source_conversion import to_source_request
from harborrag_runtime.ingestion import DocumentReleaseService, SourceIngestionService
from harborrag_runtime.ingestion.source.tasks import pending_source_task
from harborrag_runtime.ingestion_contracts import PreparedSourceSubmission, RunSettings
from harborrag_runtime.temporal.gateway import to_temporal_source
from harborrag_runtime.temporal.schemas import (
    ProcessingProfileInput,
    SourceIngestionInput,
    SourceQuery,
)
from harborrag_runtime.temporal.task_registry import (
    IngestionTaskRegistry,
)

from ...fixtures.release import (
    build_control_plane,
    build_dependencies,
    build_relation_repair_service,
    build_release_resources,
)

pytestmark = pytest.mark.unit

ACTOR_KEYS = ("actor", "actor_type")


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


def _api_submission() -> PreparedSourceSubmission:
    return PreparedSourceSubmission(
        task_id="run-1",
        tenant_id="tenant-1",
        connector_name="jira-main",
        connector_type="jira",
        connection_id="default/jira-main",
        source_scope_id="jira-scope",
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
            updated_after="2026-11-01T08:30:00+00:00",
            filters_json='{"projects":["OPS"]}',
        ),
        run_settings=RunSettings("rev_3", None, 1200, None),
    )


def _as_worker_receives(source: PreparedSourceSubmission) -> SourceIngestionInput:
    """Send ``source`` through the Temporal payload converter the workflow input uses."""
    converter = DataConverter.default.payload_converter
    (decoded,) = converter.from_payloads(
        converter.to_payloads([to_temporal_source(source)]), [SourceIngestionInput]
    )
    return decoded


async def _worker_begins_discovery(
    control: IngestionControlPlaneDatabase, source: SourceIngestionInput
) -> None:
    resources = build_release_resources(control)
    dependencies = build_dependencies(resources)
    service = SourceIngestionService(
        control=control,
        documents=DocumentReleaseService(dependencies),
        relations=build_relation_repair_service(resources, dependencies),
    )
    await service.begin_discovery(to_source_request(source))


@pytest.mark.asyncio
async def test_worker_runs_an_api_started_run_that_records_its_actor(tmp_path: Path) -> None:
    async with build_control_plane(tmp_path) as control:
        registry = IngestionTaskRegistry(control)
        source = _api_submission()
        await registry.register(source, actor="dev", actor_type="user")

        await _worker_begins_discovery(control, _as_worker_receives(source))

        task = await control.tasks.get("run-1")
        assert task is not None
        assert task.status is IngestionTaskState.RUNNING
        assert (task.request["actor"], task.request["actor_type"]) == ("dev", "user")


@pytest.mark.asyncio
async def test_retry_run_registered_again_without_its_actor_is_the_same_run(
    tmp_path: Path,
) -> None:
    async with build_control_plane(tmp_path) as control:
        registry = IngestionTaskRegistry(control)
        original = (await registry.register(_api_submission())).task
        retry = await registry.register_retry(
            retry_task_id="retry-1",
            original=original,
            document_ids=["doc-1"],
            actor="dev",
            actor_type="user",
        )
        anonymous = {
            key: value for key, value in retry.task.request.items() if key not in ACTOR_KEYS
        }

        again = await control.tasks.register(retry.task.model_copy(update={"request": anonymous}))

        assert again.created is False
        assert again.task.request["actor"] == "dev"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "differ",
    [
        lambda source: replace(source, source_scope_id="other-scope"),
        lambda source: replace(source, query=replace(source.query, path="other/path")),
    ],
    ids=["source_scope_id", "query"],
)
async def test_a_different_request_under_the_same_task_id_still_collides(
    tmp_path: Path, differ: Callable[[PreparedSourceSubmission], PreparedSourceSubmission]
) -> None:
    async with build_control_plane(tmp_path) as control:
        source = _api_submission()
        await IngestionTaskRegistry(control).register(source, actor="dev", actor_type="user")
        worker_request = to_source_request(_as_worker_receives(differ(source)))

        with pytest.raises(HarborConflictError, match="ingestion task identity collision"):
            await control.tasks.create(pending_source_task(worker_request))
