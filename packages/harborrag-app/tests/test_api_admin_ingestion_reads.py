"""``GET /v1/admin/ingestions`` and ``/{ingestion_id}`` read runs as ``Ingestion`` under editor."""

from __future__ import annotations

import base64
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient
from test_api_admin_connections import _BODY, _PATH, _SECRET, _token

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_core.domain.project import Project
from harborrag_core.ingestion import (
    DEFAULT_ACTOR_TYPE,
    IngestionTask,
    IngestionTaskState,
    TaskPage,
    TaskTrigger,
)
from harborrag_engine.control_plane.connection_views import IngestionLinks
from harborrag_engine.ingestion.operations import IngestionOperations
from harborrag_runtime.testing.connection_fakes import ConnectionWorld, connection_world

pytestmark = pytest.mark.unit

_LIST = "/v1/admin/ingestions"
_KEYS = {
    "ingestion_id",
    "tenant_id",
    "project_id",
    "connection_id",
    "mode",
    "status",
    "counters",
    "stages",
    "error_category",
    "triggered_by",
    "settings_revision",
    "workflow_id",
    "run_id",
    "trace_id",
    "links",
    "created_at",
    "finished_at",
}
_T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


class _Runs:
    """In-memory run registry with the listing filters of the real one."""

    def __init__(self) -> None:
        self.tasks: list[IngestionTask] = []

    def add(
        self, task_id: str, *, tenant: str, source_key: str, status: IngestionTaskState, age: int
    ) -> None:
        self.tasks.append(
            IngestionTask(
                task_id=task_id,
                source_scope_id=f"scope-{source_key}",
                status=status,
                request={"tenant_id": tenant, "connection_id": source_key},
                submitted_at=_T0 + timedelta(minutes=age),
            )
        )

    def started_by(self, task_id: str, *, actor_type: str, actor: str) -> None:
        """Record who started ``task_id`` the way the task registry does."""
        self.tasks = [
            task.model_copy(
                update={"request": {**task.request, "actor_type": actor_type, "actor": actor}}
            )
            if task.task_id == task_id
            else task
            for task in self.tasks
        ]

    async def source_keys(self, *, tenant_id: str) -> frozenset[str]:
        return frozenset(
            str(task.request["connection_id"])
            for task in self.tasks
            if task.request["tenant_id"] == tenant_id
        )

    async def get(self, task_id: str) -> IngestionTask | None:
        return next((task for task in self.tasks if task.task_id == task_id), None)

    async def progress(self, task_id: str) -> dict[str, int]:
        return {"published": 1}

    async def progress_for_tasks(self, task_ids: Sequence[str]) -> dict[str, dict[str, int]]:
        return {task_id: {"published": 1} for task_id in task_ids}

    async def list_tasks(  # noqa: PLR0913 - mirrors the registry
        self,
        *,
        tenant_ids: frozenset[str] | None = None,
        statuses: Sequence[str] | None = None,
        before_submitted_at: datetime | None = None,
        before_task_id: str | None = None,
        scope_connection_ids: frozenset[str] | None = None,
        created_after: datetime | None = None,
        trigger: TaskTrigger | None = None,
        limit: int = 50,
    ) -> TaskPage:
        found = sorted(
            (
                task
                for task in self.tasks
                if (tenant_ids is None or task.request["tenant_id"] in tenant_ids)
                and (statuses is None or task.status.value in statuses)
                and (
                    scope_connection_ids is None
                    or task.request["connection_id"] in scope_connection_ids
                )
                and (created_after is None or task.submitted_at >= created_after)
                and (before_submitted_at is None or task.submitted_at < before_submitted_at)
                and (trigger is None or _started(task, trigger))
            ),
            key=lambda task: task.submitted_at,
            reverse=True,
        )
        return TaskPage(tuple(found[:limit]), has_more=len(found) > limit)


def _started(task: IngestionTask, trigger: TaskTrigger) -> bool:
    """Whether ``trigger`` started ``task``; a run that records no actor type is a user's."""
    actor_type = task.request.get("actor_type") or DEFAULT_ACTOR_TYPE
    return actor_type == trigger.actor_type and trigger.actor in (None, task.request.get("actor"))


class _App(MockAppService):
    def __init__(self, world: ConnectionWorld, runs: _Runs) -> None:
        super().__init__()
        self.world = world
        self.connection_service = world.service
        self.ingestion_operations = IngestionOperations(
            connections=lambda: world.control_plane.connections,
            runs=_provide(runs),
            links=IngestionLinks(None, "default", None),
            actions=world.submitter,  # type: ignore[arg-type]
        )


def _provide(runs: _Runs):  # type: ignore[no-untyped-def]
    async def provider() -> _Runs:
        return runs

    return provider


@pytest.fixture
def runs() -> _Runs:
    return _Runs()


@pytest.fixture
def world() -> ConnectionWorld:
    return connection_world([Project(id="demo", name="DEMO", collection="demo", tenant_id="acme")])


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, runs: _Runs, world: ConnectionWorld
) -> Iterator[TestClient]:
    app_service = _App(world, runs)
    monkeypatch.setattr(api_app, "select_app_service", lambda: (app_service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_SECRET))
    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = f"Bearer {_token('editor', ['acme', 'globex'])}"
        test_client.headers["X-Tenant-Id"] = "acme"
        assert test_client.put(_PATH, json=_BODY).status_code == 201
        wiki = "/v1/admin/projects/demo/connections/demo-wiki"
        assert test_client.put(wiki, json=_BODY).status_code == 201
        keys = {
            c.connection_id: c.source_key
            for c in world.control_plane.connections.connections.values()
        }
        done, failed = IngestionTaskState.COMPLETED, IngestionTaskState.FAILED
        runs.add("ing_old", tenant="acme", source_key=keys["demo-tickets"], status=done, age=0)
        runs.add("ing_bad", tenant="acme", source_key=keys["demo-tickets"], status=failed, age=1)
        runs.add("ing_wiki", tenant="acme", source_key=keys["demo-wiki"], status=done, age=2)
        runs.add("ing_other", tenant="globex", source_key="x/y", status=done, age=3)
        yield test_client


def _ids(response) -> list[str]:  # type: ignore[no-untyped-def]
    return [item["ingestion_id"] for item in response.json()["items"]]


def test_list_returns_the_ingestion_shape_newest_first_for_the_tenant(client: TestClient) -> None:
    response = client.get(_LIST)

    assert response.status_code == 200
    assert _ids(response) == ["ing_wiki", "ing_bad", "ing_old"]
    assert set(response.json()["items"][0]) == _KEYS
    assert response.json()["items"][0]["connection_id"] == "demo-wiki"
    assert response.json()["next_cursor"] is None


def test_status_filter_keeps_one_status(client: TestClient) -> None:
    assert _ids(client.get(_LIST, params={"status": "failed"})) == ["ing_bad"]


def test_status_outside_the_spec_enum_is_a_422_on_status(client: TestClient) -> None:
    response = client.get(_LIST, params={"status": "done"})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "status"


def test_connection_ref_and_project_filters(client: TestClient) -> None:
    assert _ids(client.get(_LIST, params={"connection_ref": "demo/demo-tickets"})) == [
        "ing_bad",
        "ing_old",
    ]
    assert _ids(client.get(_LIST, params={"project_id": "demo"})) == [
        "ing_wiki",
        "ing_bad",
        "ing_old",
    ]
    assert _ids(client.get(_LIST, params={"connection_ref": "demo/missing"})) == []
    assert _ids(client.get(_LIST, params={"project_id": "nope"})) == []


def test_a_deleted_connections_runs_stay_listed_under_its_ref_and_project(
    client: TestClient, world: ConnectionWorld
) -> None:
    stored = world.control_plane.connections.connections
    for pk in [pk for pk, row in stored.items() if row.connection_id == "demo-tickets"]:
        del stored[pk]

    by_ref = client.get(_LIST, params={"connection_ref": "demo/demo-tickets"})

    assert _ids(by_ref) == ["ing_bad", "ing_old"]
    assert {(item["project_id"], item["connection_id"]) for item in by_ref.json()["items"]} == {
        ("demo", "demo-tickets")
    }
    assert _ids(client.get(_LIST, params={"project_id": "demo"})) == [
        "ing_wiki",
        "ing_bad",
        "ing_old",
    ]
    assert _ids(client.get(_LIST, params={"project_id": "nope"})) == []


@pytest.fixture
def started(runs: _Runs) -> _Runs:
    """The runs, with ``ing_wiki`` started by an API key and ``ing_bad`` by a schedule."""
    runs.started_by("ing_wiki", actor_type="api_key", actor="key_1")
    runs.started_by("ing_bad", actor_type="schedule", actor="tickets-hourly")
    return runs


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"trigger": "user"}, ["ing_old"]),
        ({"trigger": "api_key"}, ["ing_wiki"]),
        ({"trigger": "schedule"}, ["ing_bad"]),
        ({"schedule_id": "tickets-hourly"}, ["ing_bad"]),
        ({"trigger": "schedule", "schedule_id": "tickets-hourly"}, ["ing_bad"]),
        ({"schedule_id": "wiki-nightly"}, []),
        ({"trigger": "user", "schedule_id": "tickets-hourly"}, []),
    ],
)
def test_trigger_and_schedule_filters_keep_the_runs_that_starter_began(
    client: TestClient, started: _Runs, params: dict[str, str], expected: list[str]
) -> None:
    response = client.get(_LIST, params=params)

    assert response.status_code == 200
    assert _ids(response) == expected


def test_a_run_a_schedule_started_names_the_schedule(client: TestClient, started: _Runs) -> None:
    (item,) = client.get(_LIST, params={"schedule_id": "tickets-hourly"}).json()["items"]

    assert item["triggered_by"] == {"principal_type": "schedule", "principal_id": "tickets-hourly"}


def test_trigger_outside_the_spec_enum_is_a_422_on_trigger(client: TestClient) -> None:
    response = client.get(_LIST, params={"trigger": "robot"})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "trigger"


@pytest.mark.parametrize("other", [{"trigger": "api_key"}, {"schedule_id": "tickets-hourly"}])
def test_cursor_is_bound_to_its_trigger_and_schedule_filters(
    client: TestClient, other: dict[str, str]
) -> None:
    first = client.get(_LIST, params={"limit": 2, "trigger": "user"})
    cursor = first.json()["next_cursor"]

    second = client.get(_LIST, params={"limit": 2, "trigger": "user", "cursor": cursor})
    moved = client.get(_LIST, params={"limit": 2, "cursor": cursor, **other})

    assert _ids(first) == ["ing_wiki", "ing_bad"]
    assert _ids(second) == ["ing_old"]
    assert moved.status_code == 422
    assert moved.json()["error"]["details"][0]["field"] == "cursor"


def test_created_after_cuts_older_runs(client: TestClient) -> None:
    cutoff = (_T0 + timedelta(minutes=1)).isoformat()

    assert _ids(client.get(_LIST, params={"created_after": cutoff})) == ["ing_wiki", "ing_bad"]


def test_cursor_walks_pages_and_is_bound_to_its_filters(client: TestClient) -> None:
    first = client.get(_LIST, params={"limit": 2})
    cursor = first.json()["next_cursor"]
    second = client.get(_LIST, params={"limit": 2, "cursor": cursor})
    other_filter = client.get(_LIST, params={"limit": 2, "cursor": cursor, "status": "failed"})

    assert _ids(first) == ["ing_wiki", "ing_bad"]
    assert _ids(second) == ["ing_old"]
    assert other_filter.status_code == 422
    assert other_filter.json()["error"]["details"][0]["field"] == "cursor"


def test_garbage_cursor_is_a_422_on_cursor(client: TestClient) -> None:
    response = client.get(_LIST, params={"cursor": "not-base64"})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "cursor"


def without_offset(cursor: str) -> str:
    """Return ``cursor`` with its timestamps re-encoded without a UTC offset."""
    bound, separator, packed = cursor.rpartition(".")
    payload = base64.urlsafe_b64decode(packed + "=" * (-len(packed) % 4)).decode()
    naive = payload.replace("+00:00", "")
    assert naive != payload
    return f"{bound}{separator}{base64.urlsafe_b64encode(naive.encode()).decode().rstrip('=')}"


def test_cursor_without_a_utc_offset_is_a_422_on_cursor(client: TestClient) -> None:
    cursor = client.get(_LIST, params={"limit": 2}).json()["next_cursor"]

    response = client.get(_LIST, params={"limit": 2, "cursor": without_offset(cursor)})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "cursor"


@pytest.mark.parametrize("created_after", ["2026-11-01", "2026-11-01T09:00:00"])
def test_created_after_without_a_utc_offset_is_a_422_on_created_after(
    client: TestClient, created_after: str
) -> None:
    response = client.get(_LIST, params={"created_after": created_after})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "created_after"


def test_limit_above_100_is_rejected(client: TestClient) -> None:
    assert client.get(_LIST, params={"limit": 101}).status_code == 422


def test_reader_token_is_forbidden(client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {_token('reader', ['acme'])}"}

    assert client.get(_LIST, headers=headers).status_code == 403
    assert client.get(f"{_LIST}/ing_old", headers=headers).status_code == 403


def test_get_reads_one_run_with_its_connection(client: TestClient) -> None:
    response = client.get(f"{_LIST}/ing_bad")

    assert response.status_code == 200
    assert set(response.json()) == _KEYS
    assert response.json()["status"] == "failed"
    assert response.json()["project_id"] == "demo"


def test_a_run_of_another_tenant_is_not_found_and_not_listed(client: TestClient) -> None:
    assert client.get(f"{_LIST}/ing_other").status_code == 404
    assert "ing_other" not in _ids(client.get(_LIST))
    assert client.get(f"{_LIST}/missing").status_code == 404
