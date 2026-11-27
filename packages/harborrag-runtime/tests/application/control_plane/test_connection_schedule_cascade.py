"""Deleting a connection removes its schedules before any of its data."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from harborrag_adapters.connectors.policies.network import DestinationPolicy
from harborrag_core.contracts.errors import HarborNotFoundError, HarborUnavailableError
from harborrag_core.domain.project import Project
from harborrag_core.testing.fakes import FakeSecrets
from harborrag_runtime.application.control_plane.connection_models import ConnectionKey
from harborrag_runtime.application.control_plane.connections import ConnectionService
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.testing.connection_fakes import (
    CONNECTION_KEY,
    JIRA_WRITE,
    Removal,
    Runs,
    Submitter,
    fake_control_plane,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@dataclass
class _ScheduleRemoval:
    """Records each connection whose schedules it removed, and what was removed before."""

    removal: Removal
    unavailable: bool = False
    removed: list[tuple[ConnectionKey, int]] = field(default_factory=list)

    async def __call__(self, key: ConnectionKey) -> None:
        self.removed.append((key, len(self.removal.calls)))
        if self.unavailable:
            self.unavailable = False
            raise HarborUnavailableError("timer engine down")


def _service() -> tuple[ConnectionService, _ScheduleRemoval, Removal]:
    control_plane = fake_control_plane(
        [Project(id="demo", name="DEMO", collection="demo", tenant_id="acme")], FakeSecrets()
    )
    runs, removal = Runs(), Removal()
    schedules = _ScheduleRemoval(removal)

    async def provide_runs() -> Runs:
        return runs

    service = ConnectionService(
        control_plane=lambda: control_plane,
        runs=provide_runs,
        ingestions=Submitter(runs),
        removal=lambda: removal,
        destination_policy=DestinationPolicy(),
        instance_id="test",
        settings=RuntimeSettings(),
        schedule_removal=schedules,
    )
    return service, schedules, removal


async def test_delete_removes_the_schedules_before_the_documents() -> None:
    service, schedules, removal = _service()
    await service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")

    await service.delete(CONNECTION_KEY, actor="u")
    await service.aclose()

    assert schedules.removed == [(CONNECTION_KEY, 0)]
    assert len(removal.calls) == 1
    with pytest.raises(HarborNotFoundError):
        await service.get(CONNECTION_KEY)


async def test_an_unreachable_timer_keeps_the_data_and_the_removal_resumes() -> None:
    service, schedules, removal = _service()
    await service.put(CONNECTION_KEY, JIRA_WRITE, actor="u")
    schedules.unavailable = True

    await service.delete(CONNECTION_KEY, actor="u")
    await service.aclose()
    view, _ = await service.get(CONNECTION_KEY)

    assert view["status"] == "deleting" and removal.calls == []
    assert await service.resume_deletions() == 1
    assert [key for key, _ in schedules.removed] == [CONNECTION_KEY, CONNECTION_KEY]
    assert len(removal.calls) == 1
