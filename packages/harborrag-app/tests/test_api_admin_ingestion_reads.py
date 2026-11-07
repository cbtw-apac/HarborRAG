"""``GET /v1/admin/ingestions`` and ``/{ingestion_id}`` read runs as ``Ingestion`` under editor."""

from __future__ import annotations

import base64
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from app_test_connections import ConnectionWorld, connection_world
from app_test_fixtures import MockAppService
from fastapi.testclient import TestClient
from test_api_admin_connections import _BODY, _PATH, _SECRET, _token

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.control_plane.connection_views import IngestionLinks
from harborrag_app.workflow_control.ingestion.operations import IngestionOperations
from harborrag_core.domain.project import Project
from harborrag_core.ingestion import IngestionTask, IngestionTaskState, TaskPage

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
            ),
            key=lambda task: task.submitted_at,
            reverse=True,
        )
        return TaskPage(tuple(found[:limit]), has_more=len(found) > limit)


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
def client(monkeypatch: pytest.MonkeyPatch, runs: _Runs) -> Iterator[TestClient]:
    world = connection_world([Project(id="cpm", name="CPM", collection="cpm", tenant_id="acme")])
    app_service = _App(world, runs)
    monkeypatch.setattr(api_app, "select_app_service", lambda: (app_service, "test"))
    app = create_fastapi_app(ApiSettings(auth_mode="hmac", auth_secret=_SECRET))
    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = f"Bearer {_token('editor', ['acme', 'globex'])}"
        test_client.headers["X-Tenant-Id"] = "acme"
        assert test_client.put(_PATH, json=_BODY).status_code == 201
        wiki = "/v1/admin/projects/cpm/connections/cpm-wiki"
        assert test_client.put(wiki, json=_BODY).status_code == 201
        keys = {
            c.connection_id: c.source_key
            for c in world.control_plane.connections.connections.values()
        }
        done, failed = IngestionTaskState.COMPLETED, IngestionTaskState.FAILED
        runs.add("ing_old", tenant="acme", source_key=keys["cpm-tickets"], status=done, age=0)
        runs.add("ing_bad", tenant="acme", source_key=keys["cpm-tickets"], status=failed, age=1)
        runs.add("ing_wiki", tenant="acme", source_key=keys["cpm-wiki"], status=done, age=2)
        runs.add("ing_other", tenant="globex", source_key="x/y", status=done, age=3)
        yield test_client


def _ids(response) -> list[str]:  # type: ignore[no-untyped-def]
    return [item["ingestion_id"] for item in response.json()["items"]]


def test_list_returns_the_ingestion_shape_newest_first_for_the_tenant(client: TestClient) -> None:
    response = client.get(_LIST)

    assert response.status_code == 200
    assert _ids(response) == ["ing_wiki", "ing_bad", "ing_old"]
    assert set(response.json()["items"][0]) == _KEYS
    assert response.json()["items"][0]["connection_id"] == "cpm-wiki"
    assert response.json()["next_cursor"] is None


def test_status_filter_keeps_one_status(client: TestClient) -> None:
    assert _ids(client.get(_LIST, params={"status": "failed"})) == ["ing_bad"]


def test_status_outside_the_spec_enum_is_a_422_on_status(client: TestClient) -> None:
    response = client.get(_LIST, params={"status": "done"})

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "status"


def test_connection_ref_and_project_filters(client: TestClient) -> None:
    assert _ids(client.get(_LIST, params={"connection_ref": "cpm/cpm-tickets"})) == [
        "ing_bad",
        "ing_old",
    ]
    assert _ids(client.get(_LIST, params={"project_id": "cpm"})) == [
        "ing_wiki",
        "ing_bad",
        "ing_old",
    ]
    assert _ids(client.get(_LIST, params={"connection_ref": "cpm/missing"})) == []
    assert _ids(client.get(_LIST, params={"project_id": "nope"})) == []


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
    assert response.json()["project_id"] == "cpm"


def test_a_run_of_another_tenant_is_not_found_and_not_listed(client: TestClient) -> None:
    assert client.get(f"{_LIST}/ing_other").status_code == 404
    assert "ing_other" not in _ids(client.get(_LIST))
    assert client.get(f"{_LIST}/missing").status_code == 404
