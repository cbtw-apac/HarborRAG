"""One error envelope with catalog codes on every route (Story 2 AC1–AC5)."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterator

import asyncpg
import pytest
from app_test_connections import connection_world
from app_test_fixtures import MockAppService
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.control_plane.projects import ProjectService
from harborrag_core.contracts.errors import (
    HarborConflictError,
    HarborValidationError,
)
from harborrag_core.domain.project import Project

_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_LIMIT = 1_048_576


class _ProjectApp(MockAppService):
    """Mock app service with real project and connection services over fakes."""

    def __init__(self) -> None:
        super().__init__()
        self.world = connection_world(
            [Project(id="cpm", name="CPM", collection="cpm", tenant_id="DEFAULT")]
        )
        self.connection_service = self.world.service
        self.project_service = ProjectService(
            control_plane=lambda: self.world.control_plane,
            runs=self.world.service._runs,
            connections=self.world.service,
        )


@pytest.fixture
def service() -> _ProjectApp:
    return _ProjectApp()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, service: _ProjectApp) -> Iterator[TestClient]:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    app = create_fastapi_app(ApiSettings())
    failing = APIRouter()

    @failing.get("/v1/test/unhandled")
    async def unhandled() -> None:
        raise RuntimeError("database password is hunter2")

    @failing.get("/v1/test/conflict")
    async def conflict() -> None:
        raise HarborConflictError("the resource changed state")

    @failing.get("/v1/test/fields")
    async def fields() -> None:
        raise HarborValidationError(
            "bad fields", {"errors": [{"field": "config.jql", "issue": "is empty"}]}
        )

    app.include_router(failing)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _error(response: object) -> dict[str, object]:
    body = response.json()  # type: ignore[attr-defined]
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details", "trace_id"}
    assert response.headers["content-type"] == "application/json"  # type: ignore[attr-defined]
    assert _TRACE_ID.fullmatch(body["error"]["trace_id"])
    assert body["error"]["trace_id"] == response.headers["x-trace-id"]  # type: ignore[attr-defined]
    return body["error"]


def test_an_unknown_route_is_not_found_with_a_trace_id(client: TestClient) -> None:
    response = client.get("/v1/does-not-exist")

    assert response.status_code == 404
    error = _error(response)
    assert error["code"] == "not_found"
    assert error["details"] is None


def test_every_failing_field_is_listed(client: TestClient) -> None:
    response = client.put("/v1/admin/projects/p2", json={"name": 123, "extra": True})

    assert response.status_code == 422
    error = _error(response)
    assert error["code"] == "validation_error"
    assert {item["field"] for item in error["details"]} >= {"name", "extra"}
    assert all(set(item) == {"field", "issue"} for item in error["details"])
    assert "detail" not in response.json()


def test_malformed_json_is_a_validation_error_on_the_body(client: TestClient) -> None:
    response = client.put(
        "/v1/admin/projects/p2",
        content=b'{"name":',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    error = _error(response)
    assert error["code"] == "validation_error"
    assert error["details"][0]["field"] == "body"


def test_a_body_over_one_mebibyte_is_refused_before_any_write(
    client: TestClient, service: _ProjectApp
) -> None:
    padding = '{"name": "p2", "description": "' + "x" * _LIMIT + '"}'
    over = padding.encode()[: _LIMIT + 1]
    exact = padding.encode()[:_LIMIT]

    refused = client.put(
        "/v1/admin/projects/p2", content=over, headers={"Content-Type": "application/json"}
    )
    allowed = client.put(
        "/v1/admin/projects/p2", content=exact, headers={"Content-Type": "application/json"}
    )

    assert refused.status_code == 413
    error = _error(refused)
    assert error["code"] == "payload_too_large"
    assert error["details"] is None
    assert allowed.status_code != 413
    assert client.get("/v1/admin/projects/p2").status_code == 404


async def _asyncpg_refused() -> None:
    await asyncpg.connect("postgresql://u:p@127.0.0.1:1/harborrag", timeout=2)


async def _engine_refused() -> None:
    engine = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/harborrag")
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def _sqlite_locked() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT * FROM missing_table"))
    finally:
        await engine.dispose()


@pytest.mark.parametrize("failure", [_asyncpg_refused, _engine_refused], ids=["driver", "pool"])
def test_a_stopped_postgres_is_a_named_unavailable_dependency(
    client: TestClient,
    service: _ProjectApp,
    monkeypatch: pytest.MonkeyPatch,
    failure: Callable[[], Awaitable[None]],
) -> None:
    async def refused(_self: object, **_: object) -> list[Project]:
        await failure()
        return []

    monkeypatch.setattr(type(service.world.control_plane.projects), "list", refused)

    response = client.get("/v1/admin/projects")

    assert response.status_code == 503
    error = _error(response)
    assert error["code"] == "dependency_unavailable"
    assert error["details"] == {"dependency": "postgres"}


def test_a_sqlite_failure_is_not_reported_as_postgres(
    client: TestClient, service: _ProjectApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(_self: object, **_: object) -> list[Project]:
        await _sqlite_locked()
        return []

    monkeypatch.setattr(type(service.world.control_plane.projects), "list", broken)

    response = client.get("/v1/admin/projects")

    assert response.status_code == 500
    assert _error(response)["code"] == "internal_error"


def test_an_unhandled_error_is_internal_and_leaks_nothing(client: TestClient) -> None:
    response = client.get("/v1/test/unhandled")

    assert response.status_code == 500
    error = _error(response)
    assert error["code"] == "internal_error"
    assert error["details"] is None
    assert "hunter2" not in response.text


def test_a_conflict_without_its_own_code_is_an_invalid_state(client: TestClient) -> None:
    response = client.get("/v1/test/conflict")

    assert response.status_code == 409
    assert _error(response)["code"] == "invalid_state"


def test_a_domain_validation_error_lists_its_fields(client: TestClient) -> None:
    response = client.get("/v1/test/fields")

    assert response.status_code == 422
    assert _error(response)["details"] == [{"field": "config.jql", "issue": "is empty"}]


def test_a_wrong_method_keeps_405_and_allow(client: TestClient) -> None:
    response = client.post("/v1/admin/source-types")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert _error(response)["code"] == "not_found"


def test_a_chat_validation_error_uses_the_catalog(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json={})

    assert response.status_code == 422
    error = _error(response)
    assert error["code"] == "validation_error"
    assert {"field": "prompt", "issue": "Field required"} in error["details"]


def test_a_valid_client_trace_id_is_kept(client: TestClient) -> None:
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"

    response = client.get("/v1/does-not-exist", headers={"X-Trace-Id": trace_id})

    assert response.headers["x-trace-id"] == trace_id
    assert _error(response)["trace_id"] == trace_id


def test_a_traceparent_supplies_the_trace_id(client: TestClient) -> None:
    traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"

    response = client.get("/healthz", headers={"traceparent": traceparent})

    assert response.headers["x-trace-id"] == "0af7651916cd43dd8448eb211c80319c"


@pytest.mark.parametrize(
    "supplied", ["trace-123", "4BF92F3577B34DA6A3CE929D0E0E4736", "0" * 32, "x" * 64]
)
def test_an_invalid_client_trace_id_is_replaced(client: TestClient, supplied: str) -> None:
    response = client.get("/healthz", headers={"X-Trace-Id": supplied})

    assert response.headers["x-trace-id"] != supplied
    assert _TRACE_ID.fullmatch(response.headers["x-trace-id"])
