"""Run actions follow the allowed-state table, wait for the target and answer with the run."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from workflow_control_fixtures import (
    FakeTemporalClient,
    public_ingestion_command,
    service_resources,
)

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_app.workflow_control.control_plane.connection_views import IngestionLinks
from harborrag_app.workflow_control.errors import IngestionActionStateError
from harborrag_app.workflow_control.ingestion.operations import (
    ACTION_WAIT_SECONDS,
    IngestionOperations,
)
from harborrag_app.workflow_control.ingestion.service import IngestionApplicationService
from harborrag_core.ingestion import IngestionTaskState, TaskDocumentResult
from harborrag_core.testing.control_plane_fakes import FakeConnectionRepository

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
