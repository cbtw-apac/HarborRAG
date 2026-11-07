"""Run actions follow the allowed-state table, wait for the target and answer with the run."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from app_test_connections import FakeSecrets, fake_control_plane
from workflow_control_fixtures import (
    FakeTemporalClient,
    public_ingestion_command,
    service_resources,
)

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_app.workflow_control.control_plane.connection_views import IngestionLinks
from harborrag_app.workflow_control.control_plane.connections import (
    ConnectionKey,
    ConnectionService,
)
from harborrag_app.workflow_control.errors import IngestionActionStateError
from harborrag_app.workflow_control.ingestion.operations import (
    ACTION_WAIT_SECONDS,
    IngestionOperations,
)
from harborrag_app.workflow_control.ingestion.service import IngestionApplicationService
from harborrag_core.contracts.errors import HarborAlreadyRunningError
from harborrag_core.domain.connection import ConnectionBuilder
from harborrag_core.domain.project import Project
from harborrag_core.ingestion import IngestionTaskState, TaskDocumentResult
from harborrag_core.testing.control_plane_fakes import FakeConnectionRepository
from harborrag_runtime.errors import WorkflowNotFoundError
from harborrag_runtime.execution.task_registry import IngestionTaskRegistry

__all__ = ["service_resources"]

_TENANT = "ACME"
_Resources = tuple[IngestionApplicationService, IngestionControlPlaneDatabase, FakeTemporalClient]


class _Clock:
    """Stands in for ``asyncio.sleep``: records the waits and runs an optional hook."""

    def __init__(self) -> None:
        self.waits: list[float] = []
        self.on_wait: Callable[[], Any] | None = None

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        if self.on_wait is not None:
            await self.on_wait()


def _operations(resources: _Resources, clock: _Clock) -> IngestionOperations:
    service, control, _ = resources

    async def runs() -> Any:
        return control.tasks

    return IngestionOperations(
        connections=lambda: FakeConnectionRepository(),
        runs=runs,
        links=IngestionLinks(None, "default", None),
        actions=service,
        sleep=clock.sleep,
    )


async def _start(
    resources: _Resources, state: IngestionTaskState = IngestionTaskState.RUNNING
) -> str:
    service, control, _ = resources
    accepted = await service.submit(public_ingestion_command(), idempotency_key=None)
    task_id = str(accepted["task_id"])
    if state is not IngestionTaskState.PENDING:
        await control.tasks.transition(task_id, IngestionTaskState.RUNNING)
    if state is IngestionTaskState.PAUSED:
        await control.tasks.transition(task_id, IngestionTaskState.PAUSED)
    return task_id


async def _finish_with_failures(
    resources: _Resources, task_id: str, state: IngestionTaskState, failures: int
) -> None:
    _, control, _ = resources
    for index in range(failures):
        await control.tasks.record_document_result(
            TaskDocumentResult(
                task_id=task_id,
                document_id=f"document:{index}",
                status="failed",
                result={
                    "safe_error_code": "vector_write_failed",
                    "failure_stage": "WriteVectorProjection",
                    "retryable": True,
                },
            )
        )
    await control.tasks.finalize(task_id, state, summary={"stage": "COMPLETED"})


@pytest.mark.asyncio
async def test_pause_on_a_running_run_answers_with_the_paused_run(
    service_resources: _Resources,
) -> None:
    _, control, temporal = service_resources
    task_id = await _start(service_resources)
    clock = _Clock()

    async def pause_takes_effect() -> None:
        await control.tasks.transition(task_id, IngestionTaskState.PAUSED)

    clock.on_wait = pause_takes_effect

    view = await _operations(service_resources, clock).act(
        tenant_id=_TENANT, ingestion_id=task_id, action="pause", actor="u_huy"
    )

    assert view["status"] == "paused"
    assert view["ingestion_id"] == task_id
    assert temporal.paused == [task_id]
    assert clock.waits == [0.5]


@pytest.mark.asyncio
async def test_an_action_that_never_lands_answers_with_the_current_run_after_its_wait(
    service_resources: _Resources,
) -> None:
    task_id = await _start(service_resources)
    clock = _Clock()

    view = await _operations(service_resources, clock).act(
        tenant_id=_TENANT, ingestion_id=task_id, action="pause", actor="u_huy"
    )

    assert view["status"] == "running"
    assert sum(clock.waits) == ACTION_WAIT_SECONDS["pause"]
    assert set(clock.waits) == {0.5}


@pytest.mark.asyncio
async def test_resume_on_a_paused_run_answers_with_the_running_run(
    service_resources: _Resources,
) -> None:
    _, control, temporal = service_resources
    task_id = await _start(service_resources, IngestionTaskState.PAUSED)
    clock = _Clock()

    async def resume_takes_effect() -> None:
        await control.tasks.transition(task_id, IngestionTaskState.RUNNING)

    clock.on_wait = resume_takes_effect

    view = await _operations(service_resources, clock).act(
        tenant_id=_TENANT, ingestion_id=task_id, action="resume", actor="u_huy"
    )

    assert view["status"] == "running"
    assert temporal.resumed == [task_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "state"),
    [
        ("pause", IngestionTaskState.PAUSED),
        ("resume", IngestionTaskState.RUNNING),
        ("cancel", IngestionTaskState.CANCELLED),
    ],
)
async def test_an_action_outside_the_state_table_is_a_conflict_and_changes_nothing(
    service_resources: _Resources, action: str, state: IngestionTaskState
) -> None:
    _, control, temporal = service_resources
    task_id = await _start(service_resources, state)
    if state is IngestionTaskState.CANCELLED:
        await control.tasks.transition(task_id, state)

    with pytest.raises(IngestionActionStateError) as raised:
        await _operations(service_resources, _Clock()).act(
            tenant_id=_TENANT,
            ingestion_id=task_id,
            action=action,  # type: ignore[arg-type]
            actor="u_huy",
        )

    assert raised.value.error_code == "invalid_state"
    assert (temporal.paused, temporal.resumed, temporal.cancelled) == ([], [], [])
    stored = await control.tasks.get(task_id)
    assert stored is not None and stored.status is state


@pytest.mark.asyncio
async def test_cancelling_a_run_whose_workflow_was_terminated_records_the_cancellation(
    service_resources: _Resources,
) -> None:
    _, _, temporal = service_resources
    task_id = await _start(service_resources)
    temporal.statuses[task_id] = "terminated"

    view = await _operations(service_resources, _Clock()).act(
        tenant_id=_TENANT, ingestion_id=task_id, action="cancel", actor="u_huy"
    )

    assert view["status"] == "cancelled"
    assert view["finished_at"] is not None


@pytest.mark.asyncio
async def test_an_action_on_a_run_of_another_tenant_is_not_found(
    service_resources: _Resources,
) -> None:
    from harborrag_app.workflow_control.errors import IngestionNotFoundError

    task_id = await _start(service_resources)

    with pytest.raises(IngestionNotFoundError):
        await _operations(service_resources, _Clock()).act(
            tenant_id="globex", ingestion_id=task_id, action="pause", actor="u_huy"
        )


@pytest.mark.asyncio
async def test_retry_failures_starts_a_new_run_and_records_who_asked(
    service_resources: _Resources,
) -> None:
    _, _, temporal = service_resources
    task_id = await _start(service_resources)
    await _finish_with_failures(service_resources, task_id, IngestionTaskState.FAILED, 2)

    view = await _operations(service_resources, _Clock()).retry_failures(
        tenant_id=_TENANT,
        ingestion_id=task_id,
        actor="key_1",
        actor_type="api_key",
        document_ids=[],
    )

    assert view["ingestion_id"] != task_id
    assert view["status"] == "queued"
    assert view["triggered_by"] == {"principal_type": "api_key", "principal_id": "key_1"}
    assert len(temporal.retries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [IngestionTaskState.RUNNING, IngestionTaskState.PAUSED])
async def test_retry_failures_on_an_active_run_is_a_conflict(
    service_resources: _Resources, state: IngestionTaskState
) -> None:
    task_id = await _start(service_resources, state)

    with pytest.raises(IngestionActionStateError):
        await _operations(service_resources, _Clock()).retry_failures(
            tenant_id=_TENANT,
            ingestion_id=task_id,
            actor="u_huy",
            actor_type="user",
            document_ids=[],
        )


@pytest.mark.asyncio
async def test_retry_failures_on_a_run_without_failures_is_a_conflict(
    service_resources: _Resources,
) -> None:
    _, _, temporal = service_resources
    task_id = await _start(service_resources)
    await _finish_with_failures(service_resources, task_id, IngestionTaskState.COMPLETED, 0)

    with pytest.raises(IngestionActionStateError):
        await _operations(service_resources, _Clock()).retry_failures(
            tenant_id=_TENANT,
            ingestion_id=task_id,
            actor="u_huy",
            actor_type="user",
            document_ids=[],
        )

    assert temporal.retries == []


async def _retry_run(
    resources: _Resources, state: IngestionTaskState = IngestionTaskState.RUNNING
) -> str:
    """Start, fail and retry a run; return the retry run, moved to ``state``."""
    _, control, _ = resources
    task_id = await _start(resources)
    await _finish_with_failures(resources, task_id, IngestionTaskState.FAILED, 2)
    retry = await _operations(resources, _Clock()).retry_failures(
        tenant_id=_TENANT, ingestion_id=task_id, actor="u_huy", actor_type="user", document_ids=[]
    )
    retry_id = str(retry["ingestion_id"])
    if state is not IngestionTaskState.PENDING:
        await control.tasks.transition(retry_id, IngestionTaskState.RUNNING)
    if state is IngestionTaskState.PAUSED:
        await control.tasks.transition(retry_id, IngestionTaskState.PAUSED)
    return retry_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "state", "wording"),
    [
        ("pause", IngestionTaskState.RUNNING, "retry runs cannot be paused"),
        ("resume", IngestionTaskState.PAUSED, "retry runs cannot be resumed"),
    ],
)
async def test_pausing_or_resuming_a_retry_run_is_a_conflict_and_changes_nothing(
    service_resources: _Resources, action: str, state: IngestionTaskState, wording: str
) -> None:
    _, control, temporal = service_resources
    retry_id = await _retry_run(service_resources, state)

    with pytest.raises(IngestionActionStateError) as raised:
        await _operations(service_resources, _Clock()).act(
            tenant_id=_TENANT,
            ingestion_id=retry_id,
            action=action,  # type: ignore[arg-type]
            actor="u_huy",
        )

    assert raised.value.error_code == "invalid_state"
    assert wording in str(raised.value)
    assert (temporal.paused, temporal.resumed) == ([], [])
    stored = await control.tasks.get(retry_id)
    assert stored is not None and stored.status is state


@pytest.mark.asyncio
async def test_cancelling_a_running_retry_run_cancels_its_retry_workflow(
    service_resources: _Resources,
) -> None:
    _, control, temporal = service_resources
    retry_id = await _retry_run(service_resources)
    clock = _Clock()

    async def workflow_records_the_cancellation() -> None:
        await control.tasks.transition(retry_id, IngestionTaskState.CANCELLED)

    clock.on_wait = workflow_records_the_cancellation

    view = await _operations(service_resources, clock).act(
        tenant_id=_TENANT, ingestion_id=retry_id, action="cancel", actor="u_huy"
    )

    assert view["status"] == "cancelled"
    assert view["workflow_id"] == f"harborrag-retry:{retry_id}"
    assert temporal.cancelled_retries == [retry_id]
    assert temporal.cancelled == []


@pytest.mark.asyncio
async def test_cancelling_a_retry_run_whose_workflow_is_gone_records_the_cancellation(
    service_resources: _Resources,
) -> None:
    _, _, temporal = service_resources
    retry_id = await _retry_run(service_resources)
    temporal.retry_statuses[retry_id] = WorkflowNotFoundError("task not found")

    view = await _operations(service_resources, _Clock()).act(
        tenant_id=_TENANT, ingestion_id=retry_id, action="cancel", actor="u_huy"
    )

    assert view["status"] == "cancelled"
    assert view["finished_at"] is not None
    assert view["error_category"] == "workflow_not_found"


@pytest.mark.asyncio
async def test_a_retry_run_whose_workflow_was_terminated_is_settled(
    service_resources: _Resources,
) -> None:
    service, control, temporal = service_resources
    retry_id = await _retry_run(service_resources)
    temporal.retry_statuses[retry_id] = "terminated"

    assert await service.settle_if_ended(retry_id) is True

    stored = await control.tasks.get(retry_id)
    assert stored is not None and stored.status is IngestionTaskState.CANCELLED
    assert stored.summary["error_code"] == "workflow_terminated"


@pytest.mark.asyncio
async def test_a_connection_whose_only_active_run_is_a_terminated_retry_run_starts_again(
    service_resources: _Resources, tmp_path: Path
) -> None:
    service, control, temporal = service_resources
    retry_id = await _retry_run(service_resources)
    temporal.retry_statuses[retry_id] = "terminated"
    registry = IngestionTaskRegistry(control)
    active = await registry.active_for_connection(tenant_id=_TENANT, connection_id=_SOURCE_KEY)
    assert active is not None and active.task_id == retry_id
    connections = await _connection_service(service, registry, tmp_path)

    view = await connections.sync(_CONNECTION, actor="u_huy")

    assert view["ingestion_id"] != retry_id
    assert view["status"] == "queued"
    stored = await control.tasks.get(retry_id)
    assert stored is not None and stored.status is IngestionTaskState.CANCELLED


async def _older_sync_behind_terminated_retries(
    resources: _Resources, tmp_path: Path, retries: int
) -> tuple[ConnectionService, str]:
    """A RUNNING sync with ``retries`` newer retry runs whose workflows were terminated."""
    service, control, temporal = resources
    original = await _start(resources)
    await _finish_with_failures(resources, original, IngestionTaskState.FAILED, 2)
    sync = await _start(resources)
    for _ in range(retries):
        retry = await _operations(resources, _Clock()).retry_failures(
            tenant_id=_TENANT, ingestion_id=original, actor="u", actor_type="user", document_ids=[]
        )
        retry_id = str(retry["ingestion_id"])
        await control.tasks.transition(retry_id, IngestionTaskState.RUNNING)
        temporal.retry_statuses[retry_id] = "terminated"
    registry = IngestionTaskRegistry(control)
    active = await registry.active_for_connection(tenant_id=_TENANT, connection_id=_SOURCE_KEY)
    assert active is not None and active.task_id != sync
    return await _connection_service(service, registry, tmp_path), sync


@pytest.mark.asyncio
async def test_a_settled_retry_run_does_not_hide_an_older_running_sync(
    service_resources: _Resources, tmp_path: Path
) -> None:
    connections, sync = await _older_sync_behind_terminated_retries(
        service_resources, tmp_path, retries=1
    )

    busy = await connections.connection_busy(_TENANT, _SOURCE_KEY)
    with pytest.raises(HarborAlreadyRunningError) as refused:
        await connections.delete(_CONNECTION, actor="u_huy")
    await connections.aclose()

    assert busy is True
    assert refused.value.details == {"ingestion_id": sync}


@pytest.mark.asyncio
async def test_two_settled_retry_runs_do_not_let_a_sync_start_beside_an_older_one(
    service_resources: _Resources, tmp_path: Path
) -> None:
    connections, sync = await _older_sync_behind_terminated_retries(
        service_resources, tmp_path, retries=2
    )

    with pytest.raises(HarborAlreadyRunningError) as refused:
        await connections.sync(_CONNECTION, actor="u_huy")

    assert refused.value.details == {"ingestion_id": sync}


_SOURCE_KEY = "harborrag-workspace"
_CONNECTION = ConnectionKey(tenant_id=_TENANT, project_id="default", connection_id=_SOURCE_KEY)


async def _connection_service(
    ingestions: IngestionApplicationService, runs: IngestionTaskRegistry, tmp_path: Path
) -> ConnectionService:
    """A connection service over the real ingestion service and run registry."""
    plane = fake_control_plane(
        [Project(id="default", name="Default", collection="default", tenant_id=_TENANT)],
        FakeSecrets(),
    )
    await plane.connections.put(
        ConnectionBuilder.for_new(
            tenant_id=_TENANT, project_id="default", connection_id=_SOURCE_KEY
        )
        .source_type("local")
        .source_key(_SOURCE_KEY)
        .config({"source_path": str(tmp_path)})
        .build()
    )

    async def provide_runs() -> IngestionTaskRegistry:
        return runs

    return ConnectionService(
        control_plane=lambda: plane,
        runs=provide_runs,
        ingestions=ingestions,
        removal=lambda: None,  # type: ignore[arg-type,return-value]
        instance_id="test",
    )
