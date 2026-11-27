"""HTTP contract of ``/v1/admin/schedules``: roles, ETags, status codes and error mapping."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from app_test_fixtures import MockAppService
from app_test_schedules import FakeScheduleService
from fastapi.testclient import TestClient
from test_api_admin_connections import _SECRET, _token

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_core.contracts.errors import (
    HarborAlreadyRunningError,
    HarborError,
    ScheduleNotFoundError,
    UnknownReferenceError,
)
from harborrag_core.domain.schedule import ScheduleFilter
from harborrag_runtime.application.control_plane.schedule_models import (
    ScheduleKey,
    ScheduleQuery,
    ScheduleWrite,
    WriteConditions,
)
from harborrag_runtime.errors import MissingOptionalDependencyError
from harborrag_runtime.ingestion.connections import ConnectionUnavailableError

pytestmark = pytest.mark.unit

_LIST = "/v1/admin/schedules"
_PATH = "/v1/admin/schedules/demo-hourly"
_KEY = ScheduleKey("acme", "demo-hourly")
_BODY: dict[str, Any] = {
    "description": "Changed DEMO tickets every hour",
    "input": {"project_id": "demo", "connection_id": "demo-tickets"},
    "interval": "PT1H",
    "jitter": "PT5M",
}
_ROUTES = [
    ("GET", _LIST),
    ("GET", _PATH),
    ("PUT", _PATH),
    ("DELETE", _PATH),
    ("POST", f"{_PATH}/pause"),
    ("POST", f"{_PATH}/resume"),
    ("POST", f"{_PATH}/trigger"),
]


@pytest.fixture
def schedules() -> FakeScheduleService:
    return FakeScheduleService()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, schedules: FakeScheduleService) -> Iterator[TestClient]:
    service = MockAppService()
    service.schedule_service = schedules
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    settings = ApiSettings(auth_mode="hmac", auth_secret=_SECRET, http_rate_default=1000)
    with TestClient(create_fastapi_app(settings)) as test_client:
        test_client.headers["Authorization"] = f"Bearer {_token('editor', ['acme'])}"
        yield test_client


def _error(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


@pytest.mark.parametrize(("method", "path"), _ROUTES)
def test_a_reader_is_refused_before_the_service(
    client: TestClient, schedules: FakeScheduleService, method: str, path: str
) -> None:
    response = client.request(
        method,
        path,
        json=_BODY if method == "PUT" else None,
        headers={"Authorization": f"Bearer {_token('reader', ['acme'])}"},
    )

    assert _error(response) == (403, "forbidden")
    assert schedules.calls == []


def test_listing_passes_the_filters_and_the_page(
    client: TestClient, schedules: FakeScheduleService
) -> None:
    client.put(_PATH, json=_BODY)

    response = client.get(
        _LIST,
        params={
            "project_id": "demo",
            "connection_ref": "demo/demo-tickets",
            "operation": "start_ingestion",
            "state": "active",
            "due_before": "2026-12-15T10:00:00Z",
            "limit": 5,
            "cursor": "c_1",
        },
    )

    assert response.status_code == 200
    assert [item["schedule_id"] for item in response.json()["items"]] == ["demo-hourly"]
    assert response.json()["next_cursor"] is None
    assert schedules.calls[-1].arguments == {
        "tenant_id": "acme",
        "query": ScheduleQuery(
            filters=ScheduleFilter(
                project_id="demo",
                connection_id="demo-tickets",
                operation="start_ingestion",
                state="active",
                due_before=datetime(2026, 12, 15, 10, tzinfo=UTC),
            ),
            limit=5,
            cursor="c_1",
        ),
    }


@pytest.mark.parametrize(
    "params",
    [
        {"connection_ref": "demo"},
        {"connection_ref": "demo/demo-tickets/extra"},
        {"project_id": "other", "connection_ref": "demo/demo-tickets"},
    ],
)
def test_a_connection_ref_that_does_not_fit_is_refused(
    client: TestClient, schedules: FakeScheduleService, params: dict[str, str]
) -> None:
    response = client.get(_LIST, params=params)

    assert _error(response) == (422, "validation_error")
    assert [item["field"] for item in response.json()["error"]["details"]] == ["connection_ref"]
    assert schedules.calls == []


def test_put_creates_then_overwrites_with_a_new_etag(
    client: TestClient, schedules: FakeScheduleService
) -> None:
    created = client.put(_PATH, json=_BODY)
    read = client.get(_PATH)
    overwritten = client.put(_PATH, json=_BODY, headers={"If-Match": created.headers["ETag"]})

    assert (created.status_code, created.headers["ETag"]) == (201, 'W/"rev-1"')
    assert created.json()["connection_ref"] == "demo/demo-tickets"
    assert created.json()["created_by"] == "u_alex"
    assert (read.status_code, read.headers["ETag"]) == (200, 'W/"rev-1"')
    assert read.json() == created.json()
    assert (overwritten.status_code, overwritten.headers["ETag"]) == (200, 'W/"rev-2"')
    assert schedules.calls[0].arguments == {
        "key": _KEY,
        "write": ScheduleWrite(
            project_id="demo",
            connection_id="demo-tickets",
            description="Changed DEMO tickets every hour",
            interval="PT1H",
            jitter="PT5M",
        ),
        "actor": "u_alex",
        "conditions": WriteConditions(),
    }
    assert schedules.calls[-1].arguments["conditions"] == WriteConditions(expected_version=1)


def test_if_none_match_on_a_taken_id_is_already_exists(
    client: TestClient, schedules: FakeScheduleService
) -> None:
    client.put(_PATH, json=_BODY)

    response = client.put(_PATH, json=_BODY, headers={"If-None-Match": "*"})

    assert _error(response) == (409, "already_exists")
    assert schedules.calls[-1].arguments["conditions"] == WriteConditions(create_only=True)


def test_a_stale_if_match_is_precondition_failed(
    client: TestClient, schedules: FakeScheduleService
) -> None:
    client.put(_PATH, json=_BODY)

    response = client.put(_PATH, json=_BODY, headers={"If-Match": 'W/"rev-7"'})

    assert _error(response) == (412, "precondition_failed")
    assert schedules.calls[-1].arguments["conditions"] == WriteConditions(expected_version=7)


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"cron": "0 2 * * *"}, "body"),
        ({"interval": None}, "body"),
        ({"operation": "start_permission_sync"}, "operation"),
        ({"input": {**_BODY["input"], "item_refs": ["DEMO-1"]}}, "input.item_refs"),
        (
            {"input": {**_BODY["input"], "updated_after": "2026-12-01T00:00:00Z"}},
            "input.updated_after",
        ),
        ({"pause_after_failures": 21}, "pause_after_failures"),
        ({"pause_after_failures": -1}, "pause_after_failures"),
    ],
    ids=[
        "interval-and-cron",
        "neither",
        "permission-sync",
        "item-refs",
        "updated-after",
        "failures-over-20",
        "failures-negative",
    ],
)
def test_a_body_the_schema_refuses_never_reaches_the_service(
    client: TestClient, schedules: FakeScheduleService, change: dict[str, Any], field: str
) -> None:
    response = client.put(_PATH, json={**_BODY, **change})

    assert _error(response) == (422, "validation_error")
    assert field in {item["field"] for item in response.json()["error"]["details"]}
    assert schedules.calls == []


def test_a_timetable_rule_lists_each_field(client: TestClient) -> None:
    response = client.put(_PATH, json={**_BODY, "interval": "PT1M", "timezone": "Mars/Base"})

    assert _error(response) == (422, "validation_error")
    assert {item["field"] for item in response.json()["error"]["details"]} >= {
        "interval",
        "timezone",
    }


_MISSING = ScheduleNotFoundError("schedule 'demo-hourly' not found")
_DELETING = ConnectionUnavailableError("connection 'demo/demo-tickets' is being deleted")


@pytest.mark.parametrize(
    ("error", "route", "expected"),
    [
        (_MISSING, ("GET", _PATH), (404, "not_found")),
        (_MISSING, ("DELETE", _PATH), (404, "not_found")),
        (_MISSING, ("POST", f"{_PATH}/resume"), (404, "not_found")),
        (
            UnknownReferenceError(
                "connection 'demo-tickets' not found", {"field": "input.connection_id"}
            ),
            ("PUT", _PATH),
            (422, "reference_not_found"),
        ),
        (_DELETING, ("PUT", _PATH), (409, "invalid_state")),
        (
            HarborAlreadyRunningError("a run is in progress", details={"ingestion_id": "ing_92"}),
            ("POST", f"{_PATH}/trigger"),
            (409, "already_running"),
        ),
        (_DELETING, ("POST", f"{_PATH}/trigger"), (409, "invalid_state")),
    ],
    ids=[
        "get-missing",
        "delete-missing",
        "resume-missing",
        "put-unknown-connection",
        "put-deleting-connection",
        "trigger-running",
        "trigger-deleting",
    ],
)
def test_a_service_error_keeps_its_status_and_code(
    client: TestClient,
    schedules: FakeScheduleService,
    error: HarborError,
    route: tuple[str, str],
    expected: tuple[int, str],
) -> None:
    schedules.error = error
    method, path = route

    response = client.request(method, path, json=_BODY if method == "PUT" else None)

    assert _error(response) == expected
    assert response.json()["error"]["details"] == (getattr(error, "details", None) or None)


def test_delete_answers_204_and_the_schedule_is_gone(client: TestClient) -> None:
    client.put(_PATH, json=_BODY)

    deleted = client.delete(_PATH)

    assert (deleted.status_code, deleted.content) == (204, b"")
    assert _error(client.get(_PATH)) == (404, "not_found")


def test_pause_records_the_reason_and_resume_clears_the_pause(
    client: TestClient, schedules: FakeScheduleService
) -> None:
    client.put(_PATH, json=_BODY)

    paused = client.post(f"{_PATH}/pause", json={"reason": "Jira maintenance window"})
    resumed = client.post(f"{_PATH}/resume")

    assert paused.status_code == 200
    assert paused.json()["state"] == "paused"
    assert paused.json()["paused"]["reason"] == "Jira maintenance window"
    assert paused.json()["paused"]["by"] == "u_alex"
    assert (resumed.status_code, resumed.json()["state"], resumed.json()["paused"]) == (
        200,
        "active",
        None,
    )
    assert schedules.calls[-1].arguments == {"key": _KEY, "actor": "u_alex"}
    assert (paused.headers["ETag"], resumed.headers["ETag"]) == ('W/"rev-2"', 'W/"rev-3"')


def test_the_etag_of_a_pause_lets_the_next_put_through(client: TestClient) -> None:
    client.put(_PATH, json=_BODY)
    paused = client.post(f"{_PATH}/pause")

    overwritten = client.put(_PATH, json=_BODY, headers={"If-Match": paused.headers["ETag"]})

    assert (overwritten.status_code, overwritten.headers["ETag"]) == (200, 'W/"rev-3"')


def test_pause_without_a_body_passes_no_reason(
    client: TestClient, schedules: FakeScheduleService
) -> None:
    client.put(_PATH, json=_BODY)

    response = client.post(f"{_PATH}/pause")

    assert response.status_code == 200
    assert response.json()["state"] == "paused"
    assert schedules.calls[-1].arguments == {"key": _KEY, "reason": None, "actor": "u_alex"}


def test_trigger_answers_202_with_the_run_acting_as_the_schedule(client: TestClient) -> None:
    client.put(_PATH, json=_BODY)

    response = client.post(f"{_PATH}/trigger")

    assert response.status_code == 202
    assert response.headers["Location"] == f"/v1/admin/ingestions/{response.json()['ingestion_id']}"
    assert response.json()["triggered_by"] == {
        "principal_type": "schedule",
        "principal_id": "demo-hourly",
    }


class _SlotWorker:
    """App-service stand-in whose slot worker raises each scripted outcome in turn."""

    def __init__(self, *outcomes: BaseException | None) -> None:
        self.outcomes = list(outcomes)
        self.starts = 0

    async def run_schedule_slots(self) -> None:
        self.starts += 1
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome


@pytest.mark.parametrize(
    ("outcomes", "starts"),
    [
        ((None,), 1),
        ((ImportError("temporalio"),), 1),
        ((MissingOptionalDependencyError("install the temporal extra"),), 1),
        ((RuntimeError("worker lost"), RuntimeError("worker lost"), None), 3),
    ],
    ids=["returns", "no-temporal-sdk", "no-temporal-extra", "restarts-after-failures"],
)
def test_the_slot_worker_restarts_only_after_a_failure(
    monkeypatch: pytest.MonkeyPatch, outcomes: tuple[BaseException | None, ...], starts: int
) -> None:
    monkeypatch.setattr(api_app, "_SCHEDULE_SLOT_RESTART_INTERVAL_SECONDS", 0)
    worker = _SlotWorker(*outcomes)

    app = SimpleNamespace(state=SimpleNamespace(app_service=worker))

    asyncio.run(api_app._run_schedule_slots(app))  # type: ignore[arg-type]  # reads only app.state

    assert worker.starts == starts
