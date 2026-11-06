"""``/healthz`` and ``/readyz``: unauthenticated probes outside the error envelope."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.readiness import (
    ReadinessChecks,
    build_readiness_checks,
    http_ready,
    postgres_answers,
    redis_answers,
)
from harborrag_app.api.settings import ApiSettings
from harborrag_runtime.config.settings import RuntimeSettings

_SECRET = SecretStr("s" * 32)
_DEPENDENCIES = ("postgres", "redis", "temporal", "qdrant", "graph")


def _settings() -> ApiSettings:
    return ApiSettings(auth_mode="hmac", auth_secret=_SECRET)


def _constant(result: bool, calls: list[str], name: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        calls.append(name)
        return result

    return check


def _checks(failing: frozenset[str] = frozenset()) -> tuple[ReadinessChecks, list[str]]:
    calls: list[str] = []
    checks = {name: _constant(name not in failing, calls, name) for name in _DEPENDENCIES}
    return ReadinessChecks(checks, ttl_seconds=5.0), calls


def test_healthz_answers_without_a_token_or_dependency_calls() -> None:
    app = create_fastapi_app(_settings())
    readiness, calls = _checks()
    with TestClient(app) as client:
        app.state.readiness = readiness
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert calls == []


@pytest.mark.parametrize("authorization", [None, "Bearer not-a-token"])
def test_readyz_reports_every_check_whatever_the_token(authorization: str | None) -> None:
    app = create_fastapi_app(_settings())
    readiness, _ = _checks()
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(app) as client:
        app.state.readiness = readiness
        response = client.get("/readyz", headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": dict.fromkeys(_DEPENDENCIES, "ok"),
    }


def test_readyz_is_503_with_the_same_body_when_a_check_fails() -> None:
    app = create_fastapi_app(_settings())
    readiness, _ = _checks(failing=frozenset({"temporal"}))
    with TestClient(app) as client:
        app.state.readiness = readiness
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {**dict.fromkeys(_DEPENDENCIES, "ok"), "temporal": "fail"},
    }


def test_readiness_reuses_a_report_until_it_expires() -> None:
    now = [100.0]
    calls: list[str] = []
    readiness = ReadinessChecks(
        {"postgres": _constant(True, calls, "postgres")},
        ttl_seconds=5.0,
        clock=lambda: now[0],
    )

    async def scenario() -> None:
        await readiness.report()
        now[0] += 4.9
        await readiness.report()
        now[0] += 0.2
        await readiness.report()

    asyncio.run(scenario())

    assert calls == ["postgres", "postgres"]


def test_readiness_marks_a_slow_or_broken_check_as_failed() -> None:
    async def slow() -> bool:
        await asyncio.sleep(1)
        return True

    async def broken() -> bool:
        raise ConnectionRefusedError

    readiness = ReadinessChecks({"slow": slow, "broken": broken}, timeout_seconds=0.05)

    assert asyncio.run(readiness.report()) == {"slow": "fail", "broken": "fail"}


async def _serve(reply: bytes) -> tuple[asyncio.AbstractServer, int, list[bytes]]:
    received: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.read(64))
        writer.write(reply)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], received


@pytest.mark.parametrize(("reply", "ready"), [(b"N", True), (b"S", True), (b"E", False)])
def test_postgres_probe_sends_an_ssl_request(reply: bytes, ready: bool) -> None:
    async def scenario() -> tuple[bool, list[bytes]]:
        server, port, received = await _serve(reply)
        async with server:
            return await postgres_answers("127.0.0.1", port), received

    answered, received = asyncio.run(scenario())

    assert answered is ready
    assert received == [b"\x00\x00\x00\x08\x04\xd2\x16\x2f"]


@pytest.mark.parametrize(
    ("reply", "ready"),
    [(b"+PONG\r\n", True), (b"-NOAUTH Authentication required.\r\n", True), (b"-ERR\r\n", False)],
)
def test_redis_probe_pings(reply: bytes, ready: bool) -> None:
    async def scenario() -> bool:
        server, port, _ = await _serve(reply)
        async with server:
            return await redis_answers(f"redis://127.0.0.1:{port}/1")

    assert asyncio.run(scenario()) is ready


@pytest.mark.parametrize(
    ("reply", "ready"),
    [(b"HTTP/1.1 200 OK\r\n\r\n", True), (b"HTTP/1.1 503 Service Unavailable\r\n\r\n", False)],
)
def test_http_probe_reads_the_status_line(reply: bytes, ready: bool) -> None:
    async def scenario() -> tuple[bool, list[bytes]]:
        server, port, received = await _serve(reply)
        async with server:
            return await http_ready(f"http://127.0.0.1:{port}/readyz"), received

    answered, received = asyncio.run(scenario())

    assert answered is ready
    assert received[0].startswith(b"GET /readyz HTTP/1.1\r\n")


def test_probes_report_an_unreachable_dependency_as_down() -> None:
    async def scenario() -> tuple[bool, bool, bool]:
        server, port, _ = await _serve(b"")
        server.close()
        await server.wait_closed()
        return (
            await postgres_answers("127.0.0.1", port),
            await redis_answers(f"redis://127.0.0.1:{port}"),
            await http_ready(f"http://127.0.0.1:{port}/readyz"),
        )

    assert asyncio.run(scenario()) == (False, False, False)


def test_checks_cover_the_configured_dependencies() -> None:
    runtime = RuntimeSettings(
        control_db_url=SecretStr("postgresql+asyncpg://u:p@db:5432/harborrag"),
        redis_url=SecretStr("redis://cache:6379/0"),
        redis_allow_insecure_remote=True,
    )

    checks = build_readiness_checks(runtime, ApiSettings(), temporal=lambda: _true())

    assert set(checks) == set(_DEPENDENCIES)


def test_checks_leave_out_postgres_and_redis_when_not_configured() -> None:
    runtime = RuntimeSettings(control_db_url=SecretStr("sqlite+aiosqlite:///./control.db"))

    checks = build_readiness_checks(runtime, ApiSettings(), temporal=lambda: _true())

    assert set(checks) == {"temporal", "qdrant", "graph"}


async def _true() -> bool:
    return True


def test_checks_leave_out_stores_served_by_other_providers() -> None:
    runtime = RuntimeSettings(
        control_db_url=SecretStr("sqlite+aiosqlite:///./control.db"),
        vector_provider="memory",
        graph_provider="memory",
    )

    checks = build_readiness_checks(runtime, ApiSettings(), temporal=lambda: _true())

    assert set(checks) == {"temporal"}


def test_a_slow_lookup_cannot_fail_a_healthy_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    import time
    from concurrent.futures import ThreadPoolExecutor

    resolve = socket.getaddrinfo

    def lookup(host: str, *args: object, **kwargs: object) -> object:
        if host.startswith("missing-"):
            time.sleep(1.0)
            raise socket.gaierror("no such host")
        return resolve(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)

    async def scenario() -> dict[str, str]:
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        server, port, _ = await _serve(b"+PONG\r\n")
        async with server:
            checks = ReadinessChecks(
                {
                    **{
                        f"missing{index}": (
                            lambda index=index: redis_answers(f"redis://missing-{index}:6379")
                        )
                        for index in range(3)
                    },
                    "redis": lambda: redis_answers(f"redis://localhost:{port}"),
                },
                timeout_seconds=0.5,
            )
            return await checks.report()

    report = asyncio.run(scenario())

    assert report["redis"] == "ok"
    assert {report[f"missing{index}"] for index in range(3)} == {"fail"}


def test_a_host_is_reached_on_its_next_address_when_the_first_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    resolve = socket.getaddrinfo

    def lookup(host: str, port: int, *args: object, **kwargs: object) -> object:
        if host == "dual-stack":
            return [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
            ]
        return resolve(host, port, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)

    async def scenario() -> bool:
        server, port, _ = await _serve(b"+PONG\r\n")
        async with server:
            return await redis_answers(f"redis://dual-stack:{port}")

    assert asyncio.run(scenario())
