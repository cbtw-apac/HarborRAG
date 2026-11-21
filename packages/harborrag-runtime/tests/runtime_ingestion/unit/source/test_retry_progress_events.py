"""A retry run reports each failed document once, however it ends."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.ingestion_control.task_lifecycle import (
    PROGRESS_STATE_KEYS,
)
from harborrag_engine.ingestion.source.models import SourceDispatchSummary
from harborrag_runtime.application.ingestion.progress_bridge import (
    sync_ingestion_progress,
    sync_run,
)
from harborrag_runtime.events.in_process import InProcessEventBus
from harborrag_runtime.execution.task_registry import IngestionTaskRegistry
from harborrag_runtime.ingestion import SourceIngestionService
from harborrag_runtime.ingestion.source.skips import FAILURE_CURSOR_KEY, PROGRESS_SNAPSHOT_KEY

from ...fixtures.connectors import DescriptorConnector
from ...fixtures.release import (
    build_control_plane,
    build_release_resources,
    build_release_service,
    source_request,
)

pytestmark = pytest.mark.unit

_RETRY = "retry-failures"


async def _finish_retry(service: SourceIngestionService, failed: int) -> None:
    await service.finish_retry(
        _RETRY, selected=failed, summary=SourceDispatchSummary(failed=failed)
    )


async def _fail_retry(service: SourceIngestionService, failed: int) -> None:
    del failed
    await service.fail_retry(_RETRY, error_code="dispatch_runtimeerror")


@pytest.mark.asyncio
@pytest.mark.parametrize("end_retry", [_finish_retry, _fail_retry], ids=["finish", "fail"])
async def test_a_retry_run_with_failures_emits_each_document_failed_once(
    tmp_path: Path,
    end_retry: Callable[[SourceIngestionService, int], Awaitable[None]],
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        service = SourceIngestionService(
            control=control, documents=build_release_service(resources)
        )
        discovery = await service.discover(source_request("task-original"), DescriptorConnector())
        original = await control.tasks.get("task-original")
        assert original is not None
        registry = IngestionTaskRegistry(control)
        await registry.register_retry(
            retry_task_id=_RETRY,
            original=original,
            document_ids=[planned.document_id for planned in discovery.planned],
        )
        bus = InProcessEventBus()
        await sync_ingestion_progress(registry, bus)
        await service.begin_retry(_RETRY, selected=len(discovery.planned))
        for planned in discovery.planned:
            await service.record_failed_document(
                _RETRY,
                planned,
                error_type="parser_rejected_document",
                failed_stage="ParseAndNormalize",
            )
        await sync_ingestion_progress(registry, bus)

        await end_retry(service, len(discovery.planned))
        ended = await control.tasks.get(_RETRY)
        assert ended is not None
        await sync_run(registry, bus, ended)
        events = await registry.list_task_events(_RETRY)

    names = [event.name.rsplit(".", 2)[-2:] for event in events]
    assert names.count(["document", "failed"]) == len(discovery.planned) == 2
    assert names.count(["run", "completed"]) == 1


def test_the_task_store_keeps_the_keys_the_progress_bridge_writes() -> None:
    assert set(PROGRESS_STATE_KEYS) == {PROGRESS_SNAPSHOT_KEY, FAILURE_CURSOR_KEY}
