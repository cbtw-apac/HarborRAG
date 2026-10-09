"""auth_mode=none admits only loopback traffic addressed to a loopback name.

With authentication off every request is an owner of every tenant. The bind
address in settings says nothing about the socket uvicorn really bound, and a
DNS-rebinding page reaches a loopback listener with a foreign Host header, so
the guard checks each request.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.types import Receive, Scope, Send

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.loopback import (
    LoopbackOnlyMiddleware,
    is_loopback_address,
    is_loopback_host_header,
)
from harborrag_app.api.settings import ApiSettings

pytestmark = pytest.mark.blackbox

HEALTH = "/api/v1/health"
LOOPBACK = "http://127.0.0.1:8000"
LOOPBACK_PEER = ("127.0.0.1", 50000)
REBINDING_HOST = "rebind.attacker.example:8000"


def _client(
    settings: ApiSettings | None = None,
    *,
    base_url: str = LOOPBACK,
    peer: tuple[str, int] = LOOPBACK_PEER,
) -> TestClient:
    # Explicit: a stray HARBORRAG_ALLOW_INSECURE_DEV would waive the socket check.
    settings = settings or ApiSettings(allow_insecure_dev=False)
    return TestClient(create_fastapi_app(settings), base_url=base_url, client=peer)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("::ffff:127.0.0.1", True),
        ("127.0.0.2", True),
        ("0.0.0.0", False),
        ("192.0.2.10", False),
        ("::ffff:192.0.2.10", False),
        ("testclient", False),
        ("localhost.example", False),
        ("", False),
    ],
)
def test_loopback_address_recognition(host: str, expected: bool) -> None:
    assert is_loopback_address(host) is expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("localhost", True),
        ("localhost:8000", True),
        ("LOCALHOST:8000", True),
        ("127.0.0.1", True),
        ("127.0.0.1:8000", True),
        ("[::1]", True),
        ("[::1]:8000", True),
        ("localhost.", False),  # trailing dot is rejected consistently
        ("localhost.evil.example", False),
        ("127.0.0.1.nip.example", False),
        ("evil.example:80@localhost", False),
        ("localhost@evil.example", False),
        (" localhost", False),
        ("[::1", False),
        ("127.0.0.1.attacker.example", False),
        ("attacker.example", False),
        ("localhost:8000:1", False),
        ("localhost:", False),
        ("::1", False),
        ("127.0.0.2:8000", False),
        ("", False),
    ],
)
def test_loopback_host_header_recognition(header: str, expected: bool) -> None:
    assert is_loopback_host_header(header) is expected


@pytest.mark.parametrize(
    ("base_url", "peer", "host"),
    [
        (LOOPBACK, LOOPBACK_PEER, None),
        ("http://localhost:8000", LOOPBACK_PEER, None),
        (LOOPBACK, ("::1", 50000), "[::1]:8000"),
        (LOOPBACK, ("::ffff:127.0.0.1", 50000), None),
    ],
)
def test_loopback_requests_are_served(
    base_url: str, peer: tuple[str, int], host: str | None
) -> None:
    headers = {"Host": host} if host else {}

    response = _client(base_url=base_url, peer=peer).get(HEALTH, headers=headers)

    assert response.status_code == 200


@pytest.mark.parametrize("host", [REBINDING_HOST, "127.0.0.1.attacker.example"])
def test_a_foreign_host_header_is_misdirected_even_on_a_loopback_socket(host: str) -> None:
    response = _client().get(HEALTH, headers={"Host": host})

    assert response.status_code == 421
    assert response.json()["error"]["code"] == "misdirected_request"
    assert "attacker" not in response.text


def test_a_remote_peer_is_refused() -> None:
    response = _client(peer=("192.0.2.10", 41000)).get(HEALTH)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "loopback_required"


def test_a_request_on_a_non_loopback_socket_is_refused() -> None:
    # uvicorn --host 0.0.0.0 with the default HARBORRAG_HOST=127.0.0.1: the
    # local socket address is the machine's LAN address.
    response = _client(base_url="http://192.0.2.1:8000").get(
        HEALTH, headers={"Host": "localhost:8000"}
    )

    assert response.status_code == 403


def test_insecure_dev_opt_in_waives_only_the_socket_check() -> None:
    # Compose: the host browser reaches the container from the bridge gateway,
    # on the container's bridge address, but still names 127.0.0.1.
    settings = ApiSettings(host="0.0.0.0", allow_insecure_dev=True)
    client = _client(settings, base_url="http://172.18.0.5:8000", peer=("172.18.0.1", 41000))

    served = client.get(HEALTH, headers={"Host": "127.0.0.1:8000"})
    rebound = client.get(HEALTH, headers={"Host": REBINDING_HOST})

    assert served.status_code == 200
    assert rebound.status_code == 421


def test_hmac_mode_installs_no_loopback_guard() -> None:
    settings = ApiSettings(auth_mode="hmac", auth_secret=SecretStr("0123456789abcdef" * 4))

    response = _client(settings, base_url="http://192.0.2.1:8000", peer=("192.0.2.10", 41000)).get(
        HEALTH, headers={"Host": "api.example.com"}
    )

    assert response.status_code == 200


async def _asgi_status(headers: list[tuple[bytes, bytes]]) -> int:
    """Drive the middleware directly so a request can carry several Host headers."""

    seen: list[int] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            status = message["status"]
            assert isinstance(status, int)
            seen.append(status)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
    guard = LoopbackOnlyMiddleware(downstream, require_loopback_socket=True)
    await guard(scope, receive, send)  # type: ignore[arg-type]
    return seen[0]


def test_multiple_host_headers_are_misdirected() -> None:
    headers = [(b"host", b"localhost"), (b"host", b"rebind.attacker.example")]

    assert asyncio.run(_asgi_status(headers)) == 421


def test_a_missing_host_header_is_misdirected() -> None:
    assert asyncio.run(_asgi_status([])) == 421


def test_an_empty_host_header_is_misdirected() -> None:
    assert asyncio.run(_asgi_status([(b"host", b"")])) == 421


def test_an_uppercase_host_header_is_served() -> None:
    assert asyncio.run(_asgi_status([(b"host", b"LOCALHOST:8000")])) == 200


def test_a_missing_peer_or_server_address_fails_closed() -> None:
    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("must not be reached")

    async def receive() -> dict[str, object]:
        return {"type": "http.request"}

    statuses: list[object] = []

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    scope = {"type": "http", "headers": [(b"host", b"localhost")], "client": None, "server": None}
    guard = LoopbackOnlyMiddleware(downstream, require_loopback_socket=True)
    asyncio.run(guard(scope, receive, send))  # type: ignore[arg-type]

    assert statuses == [403]


@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/api/v1/health"])
def test_every_route_is_guarded_including_docs_and_health(path: str) -> None:
    settings = ApiSettings(allow_insecure_dev=False, docs_enabled=True)

    response = _client(settings).get(path, headers={"Host": REBINDING_HOST})

    assert response.status_code == 421
