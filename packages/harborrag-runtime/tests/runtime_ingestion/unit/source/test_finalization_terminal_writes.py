"""A retried finalization never ends the run early, and terminal writes keep the bridge state."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from harborrag_core.ingestion import IngestionTaskState
from harborrag_engine.ingestion.source.models import (
    PlannedDocuments,
    SourceDiscoveryRun,
    SourceDispatchSummary,
)
from harborrag_runtime.application.ingestion.progress_bridge import (
    sync_ingestion_progress,
    sync_run,
)
from harborrag_runtime.events.in_process import InProcessEventBus
from harborrag_runtime.execution.task_registry import IngestionTaskRegistry
from harborrag_runtime.ingestion import SourceIngestionService
from harborrag_runtime.ingestion.maintenance.relation_repair import RelationRepairResult

from ...fixtures.connectors import DescriptorConnector
from ...fixtures.release import (
    build_control_plane,
    build_release_resources,
    build_release_service,
    source_request,
)

pytestmark = pytest.mark.unit

_NO_REPAIRS = RelationRepairResult(
    repaired_documents=0, resolved_relations=0, unresolved_relations=0
)


class _RelationRepair:
    """Relation repair that fails its first ``failures`` passes, then succeeds."""

    def __init__(self, failures: int) -> None:
        self._failures = failures

    async def repair(self, planned: object, *, tenant_id: str) -> RelationRepairResult:
        del planned, tenant_id
        if self._failures:
            self._failures -= 1
            raise RuntimeError("repair unavailable")
        return _NO_REPAIRS

    async def repair_resolvable(
        self, *, tenant_id: str, graph_projection_version: str
    ) -> RelationRepairResult:
        del tenant_id, graph_projection_version
        return _NO_REPAIRS


async def _finish(
    service: SourceIngestionService, task_id: str, discovery: SourceDiscoveryRun
) -> object:
    return await service.finish(
        source_request(task_id),
        scan_id=discovery.scan_id,
        planned=PlannedDocuments.of(discovery.planned),
        summary=SourceDispatchSummary(published=len(discovery.planned)),
    )


@pytest.mark.asyncio
async def test_a_retried_finalization_completes_without_failing_the_run_first(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        service = SourceIngestionService(
            control=control,
            documents=build_release_service(resources),
            relations=_RelationRepair(failures=1),  # type: ignore[arg-type]  # repair calls only
        )
        discovery = await service.discover(source_request("task-retried"), DescriptorConnector())

        with pytest.raises(RuntimeError, match="repair unavailable"):
            await _finish(service, "task-retried", discovery)
        between = await control.tasks.get("task-retried")
        await _finish(service, "task-retried", discovery)
        finished = await control.tasks.get("task-retried")

    assert between is not None
    assert between.status is IngestionTaskState.RUNNING
    assert between.summary["failed_stage"] == "relation_repair"
    assert finished is not None
    assert finished.status is IngestionTaskState.COMPLETED
    assert "failed_stage" not in finished.summary


@pytest.mark.asyncio
async def test_a_finalization_out_of_retries_fails_at_its_recorded_stage(tmp_path: Path) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        service = SourceIngestionService(
            control=control,
            documents=build_release_service(resources),
            relations=_RelationRepair(failures=3),  # type: ignore[arg-type]  # repair calls only
        )
        discovery = await service.discover(source_request("task-exhausted"), DescriptorConnector())
        for _ in range(3):
            with pytest.raises(RuntimeError, match="repair unavailable"):
                await _finish(service, "task-exhausted", discovery)

        await service.fail("task-exhausted", error_code="RuntimeError")
        task = await control.tasks.get("task-exhausted")

    assert task is not None
    assert task.status is IngestionTaskState.FAILED
    assert task.summary["failed_stage"] == "relation_repair"
    assert task.summary["error_code"] == "RuntimeError"
    assert (task.summary["discovered"], task.summary["published"]) == (2, 2)


async def _finalize_as_failed(
    service: SourceIngestionService, task_id: str, discovery: SourceDiscoveryRun
) -> None:
    await service.finish(
        source_request(task_id),
        scan_id=discovery.scan_id,
        planned=PlannedDocuments.of(discovery.planned),
        summary=SourceDispatchSummary(failed=len(discovery.planned)),
    )


async def _fail_dispatch(
    service: SourceIngestionService, task_id: str, discovery: SourceDiscoveryRun
) -> None:
    del discovery
    await service.fail(task_id, error_code="dispatch_runtimeerror", stage="dispatch")


@pytest.mark.asyncio
@pytest.mark.parametrize("end_run", [_finalize_as_failed, _fail_dispatch], ids=["finalize", "fail"])
async def test_a_run_ending_never_emits_its_failed_documents_again(
    tmp_path: Path,
    end_run: Callable[[SourceIngestionService, str, SourceDiscoveryRun], Awaitable[None]],
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        service = SourceIngestionService(
            control=control, documents=build_release_service(resources)
        )
        discovery = await service.discover(source_request("task-failures"), DescriptorConnector())
        for planned in discovery.planned:
            await service.record_failed_document(
                "task-failures",
                planned,
                error_type="parser_rejected_document",
                failed_stage="ParseAndNormalize",
            )
        registry = IngestionTaskRegistry(control)
        bus = InProcessEventBus()
        await sync_ingestion_progress(registry, bus)

        await end_run(service, "task-failures", discovery)
        ended = await control.tasks.get("task-failures")
        assert ended is not None
        await sync_run(registry, bus, ended)
        events = await registry.list_task_events("task-failures")

    names = [event.name.rsplit(".", 2)[-2:] for event in events]
    assert ended.status is IngestionTaskState.FAILED
    assert names.count(["document", "failed"]) == len(discovery.planned) == 2
    assert names.count(["run", "completed"]) == 1
