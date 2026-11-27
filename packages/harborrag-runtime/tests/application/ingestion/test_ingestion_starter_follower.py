"""IngestionStarter and IngestionFollower over the connection world's services and fakes."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from harborrag_core.contracts.errors import (
    HarborAlreadyRunningError,
    HarborCapabilityError,
    HarborNotFoundError,
    UnknownReferenceError,
)
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.project import Project
from harborrag_core.ingestion import IngestionTaskState
from harborrag_runtime.application.control_plane.connection_models import ConnectionKey
from harborrag_runtime.application.control_plane.projects import ProjectService
from harborrag_runtime.application.ingestion.follower import IngestionFollower
from harborrag_runtime.application.ingestion.starter import IngestionStarter
from harborrag_runtime.testing.connection_fakes import (
    JIRA_WRITE,
    ConnectionWorld,
    connection_world,
    running_task,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_KEY = ConnectionKey("acme", "engineering", "jira-main")


@pytest_asyncio.fixture
async def world() -> ConnectionWorld:
    built = connection_world(
        [Project(id="engineering", name="Engineering", collection="eng", tenant_id="acme")]
    )
    await built.service.put(_KEY, JIRA_WRITE, actor="u_demo")
    return built


async def _start(
    starter: IngestionStarter, key: ConnectionKey = _KEY, *, item_refs: tuple[str, ...] = ()
) -> dict[str, Any]:
    return await starter.start(
        key,
        actor="u_demo",
        actor_type="user",
        mode="incremental",
        item_refs=item_refs,
        updated_after=None,
    )


@pytest.fixture
def starter(world: ConnectionWorld) -> IngestionStarter:
    return IngestionStarter(
        projects=ProjectService(
            control_plane=lambda: world.control_plane,
            runs=world.service._runs,
            connections=world.service,
        ),
        connections=world.service,
        runs=world.operations,
    )


class _Events:
    """Event source double that records the runs it was asked to stream."""

    def __init__(self) -> None:
        self.streamed: list[str] = []

    async def __call__(
        self, task_id: str, *, after_seq: int | None = None
    ) -> AsyncGenerator[HarborEvent, None]:
        self.streamed.append(task_id)
        yield HarborEvent(
            name=f"ingestion.{task_id}.run.completed", trace_id=task_id, payload={}, seq=1
        )


@pytest.fixture
def events() -> _Events:
    return _Events()


@pytest.fixture
def follower(world: ConnectionWorld, events: _Events) -> IngestionFollower:
    return IngestionFollower(runs=world.operations, events=events)


async def test_start_answers_the_queued_run_with_its_links(starter: IngestionStarter) -> None:
    run = await _start(starter)

    assert run["triggered_by"]["principal_id"] == "u_demo"
    assert "links" in run


async def test_start_as_a_schedule_reads_back_as_triggered_by_the_schedule(
    starter: IngestionStarter,
) -> None:
    run = await starter.start(
        _KEY,
        actor="demo-tickets-hourly",
        actor_type="schedule",
        mode="incremental",
        item_refs=(),
        updated_after=None,
    )

    assert run["triggered_by"] == {
        "principal_type": "schedule",
        "principal_id": "demo-tickets-hourly",
    }


async def test_start_of_an_unknown_project_names_the_field(starter: IngestionStarter) -> None:
    with pytest.raises(UnknownReferenceError) as refused:
        await _start(starter, ConnectionKey("acme", "nowhere", "jira-main"))

    assert refused.value.details == {"field": "project_id"}


async def test_start_of_an_unknown_connection_names_the_field(starter: IngestionStarter) -> None:
    with pytest.raises(UnknownReferenceError) as refused:
        await _start(starter, ConnectionKey("acme", "engineering", "missing"))

    assert refused.value.details == {"field": "connection_id"}


async def test_start_with_item_refs_is_refused_when_the_source_cannot(
    starter: IngestionStarter,
) -> None:
    with pytest.raises(HarborCapabilityError):
        await _start(starter, item_refs=("DEMO-1",))


async def test_a_second_start_while_one_runs_is_refused(
    starter: IngestionStarter, world: ConnectionWorld
) -> None:
    started = await _start(starter)
    world.runs.active = world.runs.tasks[started["ingestion_id"]]

    with pytest.raises(HarborAlreadyRunningError):
        await _start(starter)


async def test_follow_of_another_tenants_run_reads_no_event(
    follower: IngestionFollower, world: ConnectionWorld, events: _Events
) -> None:
    world.runs.tasks["ing_x"] = running_task("ing_x", IngestionTaskState.RUNNING)

    with pytest.raises(HarborNotFoundError):
        await follower.follow(tenant_id="other", ingestion_id="ing_x", after_seq=None)

    assert events.streamed == []


async def test_follow_of_a_running_run_returns_the_live_events(
    follower: IngestionFollower, world: ConnectionWorld
) -> None:
    world.runs.tasks["ing_live"] = running_task("ing_live", IngestionTaskState.RUNNING)

    stream = await follower.follow(tenant_id="acme", ingestion_id="ing_live", after_seq=None)

    assert stream is not None
    assert [event.seq async for event in stream] == [1]


async def test_follow_of_an_ended_run_yields_its_events_from_the_first(
    follower: IngestionFollower, world: ConnectionWorld
) -> None:
    world.runs.tasks["ing_done"] = running_task("ing_done", IngestionTaskState.COMPLETED)

    stream = await follower.follow(tenant_id="acme", ingestion_id="ing_done", after_seq=None)

    assert stream is not None
    assert [event.seq async for event in stream] == [1]


async def test_follow_of_an_ended_run_with_nothing_after_the_resume_point_is_no_stream(
    world: ConnectionWorld,
) -> None:
    async def nothing(
        task_id: str, *, after_seq: int | None = None
    ) -> AsyncGenerator[HarborEvent, None]:
        return
        yield

    world.runs.tasks["ing_done"] = running_task("ing_done", IngestionTaskState.COMPLETED)

    stream = await IngestionFollower(runs=world.operations, events=nothing).follow(
        tenant_id="acme", ingestion_id="ing_done", after_seq=1
    )

    assert stream is None


class _Closing:
    """An async generator stand-in that fails on the first read and records ``aclose``."""

    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self) -> _Closing:
        return self

    async def __anext__(self) -> HarborEvent:
        raise ConnectionError("event store down")

    async def aclose(self) -> None:
        self.closed = True


async def test_follow_of_an_ended_run_closes_the_events_when_the_first_read_fails(
    world: ConnectionWorld,
) -> None:
    stream = _Closing()
    world.runs.tasks["ing_done"] = running_task("ing_done", IngestionTaskState.COMPLETED)

    def events(task_id: str, *, after_seq: int | None = None) -> _Closing:
        return stream

    follower = IngestionFollower(runs=world.operations, events=events)  # type: ignore[arg-type]  # fake stream

    with pytest.raises(ConnectionError):
        await follower.follow(tenant_id="acme", ingestion_id="ing_done", after_seq=None)

    assert stream.closed
