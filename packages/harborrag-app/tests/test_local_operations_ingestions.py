"""LocalOperations runs the ingestion and failure operations over the application services."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from app_test_connections import ConnectionWorld, connection_world, running_task
from app_test_fixtures import MockAppService
from test_connection_service import JIRA_WRITE

from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.v1.admin.connection_schemas import IngestionStartRequest
from harborrag_app.api.v1.ingestion.schemas import (
    FailureQuery,
    FailureRetryRequest,
    IngestionListQuery,
)
from harborrag_app.operations import (
    Accepted,
    ConnectionRef,
    LocalOperations,
    ReferenceNotFoundError,
    Scope,
)
from harborrag_app.operations.errors import error_body
from harborrag_app.workflow_control.control_plane.projects import ProjectService
from harborrag_app.workflow_control.errors import IngestionActionStateError, IngestionNotFoundError
from harborrag_core.contracts.errors import (
    HarborAlreadyRunningError,
    HarborAuthError,
    HarborCapabilityError,
    HarborNotFoundError,
)
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.project import Project
from harborrag_core.ingestion import IngestionTaskState

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_KEY = ConnectionRef("engineering", "jira-main").key("acme")
_START = IngestionStartRequest(project_id="engineering", connection_id="jira-main")


class _Inbox:
    """Failure inbox double that records what the operation asked for."""

    def __init__(self) -> None:
        self.page_calls: list[dict[str, Any]] = []
        self.retry_calls: list[dict[str, Any]] = []

    async def page(self, **query: Any) -> dict[str, Any]:
        self.page_calls.append(query)
        return {"items": [], "next_cursor": None, "counts": {}, "total": 0}

    async def retry(self, **request: Any) -> dict[str, Any]:
        self.retry_calls.append(request)
        return {"ingestion_id": "ing_9", "ingestion_ids": ["ing_9"], "queued": 2}


class _IngestionApp(MockAppService):
    """Mock app service carrying real connection services and run reads over fakes."""

    def __init__(self, world: ConnectionWorld) -> None:
        super().__init__()
        self.connection_service = world.service
        self.project_service = ProjectService(
            control_plane=lambda: world.control_plane,
            runs=world.service._runs,
            connections=world.service,
        )
        self.ingestion_operations = world.operations
        self.failure_inbox = _Inbox()
        self.read_events: list[str] = []

    async def stream_ingestion_events(  # type: ignore[override]  # an async generator, as the service
        self, task_id: str, *, after_seq: int | None = None
    ) -> AsyncIterator[HarborEvent]:
        self.read_events.append(task_id)
        yield HarborEvent(
            name=f"ingestion.{task_id}.run.completed", trace_id=task_id, payload={}, seq=1
        )


def _scope(role: Any = "editor", tenant_id: str = "acme") -> Scope:
    return Scope(Principal("u_demo", role, frozenset({"acme", "other"})), tenant_id)


@pytest.fixture
def world() -> ConnectionWorld:
    return connection_world(
        [Project(id="engineering", name="Engineering", collection="eng", tenant_id="acme")]
    )


@pytest_asyncio.fixture
async def operations(world: ConnectionWorld) -> LocalOperations:
    await world.service.put(_KEY, JIRA_WRITE, actor="u_demo")
    return LocalOperations(_IngestionApp(world))


async def test_start_ingestion_answers_where_the_run_can_be_followed(
    operations: LocalOperations,
) -> None:
    accepted = await operations.start_ingestion(_scope(), _START)

    assert isinstance(accepted, Accepted)
    assert accepted.location == f"/v1/admin/ingestions/{accepted.body.ingestion_id}"
    assert accepted.body.triggered_by.principal_id == "u_demo"


async def test_start_ingestion_of_an_unknown_project_names_the_field(
    operations: LocalOperations,
) -> None:
    with pytest.raises(ReferenceNotFoundError) as refused:
        await operations.start_ingestion(
            _scope(), IngestionStartRequest(project_id="nowhere", connection_id="jira-main")
        )

    assert refused.value.details["field"] == "project_id"
    assert error_body(refused.value).status == 422


async def test_start_ingestion_of_an_unknown_connection_names_the_field(
    operations: LocalOperations,
) -> None:
    with pytest.raises(ReferenceNotFoundError) as refused:
        await operations.start_ingestion(
            _scope(), IngestionStartRequest(project_id="engineering", connection_id="missing")
        )

    assert refused.value.details["field"] == "connection_id"


async def test_start_ingestion_with_item_refs_is_refused_when_the_source_cannot(
    operations: LocalOperations,
) -> None:
    with pytest.raises(HarborCapabilityError):
        await operations.start_ingestion(
            _scope(),
            IngestionStartRequest(
                project_id="engineering", connection_id="jira-main", item_refs=["DEMO-1"]
            ),
        )


async def test_a_second_start_while_one_runs_is_refused(
    operations: LocalOperations, world: ConnectionWorld
) -> None:
    started = await operations.start_ingestion(_scope(), _START)
    world.runs.active = world.runs.tasks[started.body.ingestion_id]

    with pytest.raises(HarborAlreadyRunningError):
        await operations.start_ingestion(_scope(), _START)


async def test_a_reader_may_not_start_an_ingestion(operations: LocalOperations) -> None:
    with pytest.raises(HarborAuthError):
        await operations.start_ingestion(_scope("reader"), _START)


async def test_stream_ingestion_of_another_tenants_run_reads_no_event(
    operations: LocalOperations, world: ConnectionWorld
) -> None:
    world.runs.tasks["ing_x"] = running_task("ing_x", IngestionTaskState.RUNNING)
    app = operations._app_service
    assert isinstance(app, _IngestionApp)

    with pytest.raises(HarborNotFoundError):
        await operations.stream_ingestion(_scope(tenant_id="other"), "ing_x", after_seq=None)

    assert app.read_events == []


async def test_stream_ingestion_yields_the_events_of_an_ended_run_from_the_first(
    operations: LocalOperations, world: ConnectionWorld
) -> None:
    world.runs.tasks["ing_done"] = running_task("ing_done", IngestionTaskState.COMPLETED)

    events = await operations.stream_ingestion(_scope(), "ing_done", after_seq=None)

    assert events is not None
    assert [event.seq async for event in events] == [1]


async def test_pause_ingestion_of_a_finished_run_is_an_invalid_state_conflict(
    operations: LocalOperations, world: ConnectionWorld
) -> None:
    world.runs.tasks["ing_done"] = running_task("ing_done", IngestionTaskState.COMPLETED)

    with pytest.raises(IngestionActionStateError) as refused:
        await operations.pause_ingestion(_scope(), "ing_done")

    body = error_body(refused.value)
    assert (body.status, body.code) == (409, "invalid_state")


async def test_get_ingestion_of_an_unknown_run_is_not_found(operations: LocalOperations) -> None:
    with pytest.raises(IngestionNotFoundError):
        await operations.get_ingestion(_scope(), "ing_missing")


async def test_list_ingestions_answers_the_tenants_runs(operations: LocalOperations) -> None:
    started = await operations.start_ingestion(_scope(), _START)

    page = await operations.list_ingestions(_scope(), IngestionListQuery())

    assert [run.ingestion_id for run in page.items] == [started.body.ingestion_id]


async def test_list_failures_passes_the_query_and_the_tenant(operations: LocalOperations) -> None:
    page = await operations.list_failures(_scope(), FailureQuery(project_id="engineering"))

    app = operations._app_service
    assert isinstance(app, _IngestionApp)
    assert page.total == 0
    assert app.failure_inbox.page_calls[0]["tenant_id"] == "acme"
    assert app.failure_inbox.page_calls[0]["project_id"] == "engineering"


async def test_retry_failures_records_the_actor(operations: LocalOperations) -> None:
    accepted = await operations.retry_failures(_scope(), FailureRetryRequest(ids=["f1", "f2"]))

    app = operations._app_service
    assert isinstance(app, _IngestionApp)
    assert accepted.queued == 2
    assert app.failure_inbox.retry_calls[0]["actor"] == "u_demo"
    assert app.failure_inbox.retry_calls[0]["ids"] == ["f1", "f2"]
