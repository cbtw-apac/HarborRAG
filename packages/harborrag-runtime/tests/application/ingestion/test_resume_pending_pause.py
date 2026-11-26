"""Resume undoes a pause the workflow accepted but has not applied yet."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_core.contracts.errors import IngestionActionStateError
from harborrag_core.ingestion import IngestionTaskState
from harborrag_runtime.application.ingestion.service import IngestionApplicationService
from harborrag_runtime.ingestion_contracts import IngestionExecutionStatus

from ..application_fixtures import FakeTemporalClient, public_ingestion_command
from ..application_fixtures import service_resources as service_resources

pytestmark = pytest.mark.unit

_Resources = tuple[IngestionApplicationService, IngestionControlPlaneDatabase, FakeTemporalClient]


async def _running(resources: _Resources, *, paused: bool, cancel_requested: bool = False) -> str:
    service, control, temporal = resources
    accepted = await service.submit(public_ingestion_command(), idempotency_key=None)
    task_id = str(accepted["task_id"])
    await control.tasks.transition(task_id, IngestionTaskState.RUNNING)
    temporal.get_status = AsyncMock(  # type: ignore[attr-defined]
        return_value=IngestionExecutionStatus(
            task_id=task_id,
            status="PAUSED" if paused else "RUNNING",
            paused=paused,
            cancel_requested=cancel_requested,
        )
    )
    return task_id


@pytest.mark.asyncio
async def test_resume_takes_back_a_pause_sent_during_discovery(
    service_resources: _Resources,
) -> None:
    service, _, temporal = service_resources
    task_id = await _running(service_resources, paused=True)

    resumed = await service.resume(task_id)

    assert temporal.resumed == [task_id]
    assert resumed["message"] == "Resume requested"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("paused", "cancel_requested"), [(False, False), (True, True)], ids=["running", "cancelling"]
)
async def test_resume_on_a_running_run_without_a_pending_pause_is_rejected(
    service_resources: _Resources, paused: bool, cancel_requested: bool
) -> None:
    service, _, temporal = service_resources
    task_id = await _running(service_resources, paused=paused, cancel_requested=cancel_requested)

    with pytest.raises(IngestionActionStateError, match="running") as raised:
        await service.resume(task_id)
    assert raised.value.error_code == "invalid_state"
    assert temporal.resumed == []
