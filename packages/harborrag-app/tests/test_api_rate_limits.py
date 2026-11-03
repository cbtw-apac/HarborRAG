"""Per-principal rate limits and the route deadline (Story 2 AC13, requirement 1)."""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app_test_connections import connection_world
from app_test_fixtures import MockAppService
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from harborrag_app.api import app as api_app
from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.access import api_routes, require_role
from harborrag_app.api.deadline import enforce_deadline
from harborrag_app.api.rate_limit import LocalRateLimiter, RedisRateLimiter
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.control_plane.projects import ProjectService
from harborrag_core.domain.project import Project

_SECRET = "test-secret-at-least-32-bytes-long-for-hs256"


def _token(role: str, subject: str = "u_huy") -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": subject,
            "role": role,
            "tenants": ["DEFAULT"],
            "iat": now - timedelta(seconds=1),
            "exp": now + timedelta(minutes=5),
            "iss": "harborrag",
            "aud": "harborrag-api",
        },
        _SECRET,
        algorithm="HS256",
    )


def _bearer(role: str, subject: str = "u_huy") -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(role, subject)}"}


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _App(MockAppService):
    def __init__(self) -> None:
        super().__init__()
        world = connection_world(
            [Project(id="cpm", name="CPM", collection="cpm", tenant_id="DEFAULT")]
        )
        self.connection_service = world.service
        self.project_service = ProjectService(
            control_plane=lambda: world.control_plane,
            runs=world.service._runs,
            connections=world.service,
        )


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def service() -> _App:
    return _App()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, service: _App, clock: _Clock) -> Iterator[TestClient]:
    monkeypatch.setattr(api_app, "select_app_service", lambda: (service, "test"))
    settings = ApiSettings(
        auth_mode="hmac", auth_secret=_SECRET, http_rate_default=10, http_rate_chat=2
    )
    app = create_fastapi_app(settings)
    with TestClient(app) as test_client:
        app.state.rate_limiter = LocalRateLimiter(clock=clock)
        yield test_client


def test_calls_over_the_default_limit_are_refused_until_the_window_moves(
    client: TestClient, clock: _Clock
) -> None:
    responses = [client.get("/v1/admin/connections", headers=_bearer("editor")) for _ in range(11)]

    assert [response.status_code for response in responses[:10]] == [200] * 10
    refused = responses[10]
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "rate_limited"
    retry_after = int(refused.headers["retry-after"])
    assert retry_after >= 1

    clock.now += retry_after
    assert client.get("/v1/admin/connections", headers=_bearer("editor")).status_code == 200


def test_the_chat_bucket_is_separate_and_per_principal(client: TestClient) -> None:
    chat = [
        client.post("/v1/chat/completions", headers=_bearer("reader"), json={"prompt": "hi"})
        for _ in range(3)
    ]

    assert [response.status_code for response in chat] == [200, 200, 429]
    assert client.get("/v1/me", headers=_bearer("reader")).status_code == 200
    assert client.get("/v1/me", headers=_bearer("admin", subject="u_admin")).status_code == 200


def test_probes_are_never_limited(client: TestClient) -> None:
    assert {client.get("/healthz").status_code for _ in range(100)} == {200}


def test_a_refused_credential_is_not_charged(client: TestClient) -> None:
    for _ in range(20):
        assert client.get("/v1/admin/connections", headers=_bearer("reader")).status_code == 403

    assert client.get("/v1/admin/connections", headers=_bearer("editor")).status_code == 200


def test_a_call_is_charged_once_whatever_guards_it_passes(client: TestClient) -> None:
    guarded = APIRouter(
        dependencies=[Depends(require_role("reader")), Depends(require_role("editor"))]
    )

    @guarded.get("/v1/test/two-guards")
    async def two_guards() -> dict[str, bool]:
        return {"ok": True}

    client.app.include_router(guarded)  # type: ignore[attr-defined]

    statuses = [
        client.get("/v1/test/two-guards", headers=_bearer("editor")).status_code for _ in range(10)
    ]

    assert statuses == [200] * 10


def test_a_limiter_outage_lets_calls_through(client: TestClient) -> None:
    class _Down:
        async def acquire(self, key: str, limit: int) -> float | None:
            raise ConnectionError("redis is down")

        async def aclose(self) -> None:
            return None

    client.app.state.rate_limiter = _Down()  # type: ignore[attr-defined]

    assert client.get("/v1/admin/connections", headers=_bearer("editor")).status_code == 200


def test_a_handler_past_its_deadline_is_504(
    client: TestClient, service: _App, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow(**_: object) -> tuple[list[object], None]:
        await asyncio.sleep(1)
        return [], None

    monkeypatch.setattr(service.project_service, "list_page", slow)
    client.app.state.settings.api_request_timeout_seconds = 0.05  # type: ignore[attr-defined]

    response = client.get("/v1/admin/projects", headers=_bearer("admin"))

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "deadline_exceeded"


def test_every_v1_route_runs_under_the_deadline(client: TestClient) -> None:
    def calls(dependant: object) -> set[object]:
        found = {dependant.call}  # type: ignore[attr-defined]
        for child in dependant.dependencies:  # type: ignore[attr-defined]
            found |= calls(child)
        return found

    routes = [
        route
        for route in api_routes(client.app.routes)
        if route.path.startswith("/v1/")  # type: ignore[attr-defined]
    ]

    assert len(routes) >= 20
    assert all(enforce_deadline in calls(route.dependant) for route in routes)


def test_the_local_window_slides() -> None:
    clock = _Clock()
    limiter = LocalRateLimiter(clock=clock)

    async def scenario() -> list[float | None]:
        first = await limiter.acquire("k", 2)
        clock.now += 0.6
        second = await limiter.acquire("k", 2)
        clock.now += 0.3
        third = await limiter.acquire("k", 2)
        clock.now += 0.2
        fourth = await limiter.acquire("k", 2)
        return [first, second, third, fourth]

    first, second, third, fourth = asyncio.run(scenario())

    assert first is None and second is None and fourth is None
    assert third == pytest.approx(0.1)


def _redis_url() -> str | None:
    url = os.environ.get("HARBORRAG_TEST_REDIS_URL", "redis://127.0.0.1:6380/15")
    host, port = url.removeprefix("redis://").split("/")[0].split(":")
    try:
        socket.create_connection((host, int(port)), timeout=0.5).close()
    except OSError:
        return None
    return url


def test_the_redis_window_matches_the_local_one() -> None:
    url = _redis_url()
    if url is None:
        pytest.skip("no Redis to test the shared window against")
    from redis.asyncio import Redis

    async def scenario() -> list[float | None]:
        limiter = RedisRateLimiter(Redis.from_url(url), key_prefix=f"test-{os.getpid()}")
        try:
            return [await limiter.acquire("k", 2) for _ in range(3)]
        finally:
            await limiter.aclose()

    first, second, third = asyncio.run(scenario())

    assert first is None and second is None
    assert third is not None and 0 < third <= 1
