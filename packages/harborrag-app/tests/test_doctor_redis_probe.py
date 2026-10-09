"""The FalkorDB doctor probe authenticates when a password is configured."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from harborrag_app.cli.doctor import probes, suite
from harborrag_runtime.config.settings import RuntimeSettings

pytestmark = pytest.mark.unit

PASSWORD = "probe-secret"


@pytest.fixture
def requirepass_server() -> Iterator[tuple[int, list[bytes]]]:
    """A RESP server that answers like Redis/FalkorDB with requirepass set."""

    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    received: list[bytes] = []

    def serve() -> None:
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(1.0)
                data = b""
                try:
                    while b"PING\r\n" not in data:
                        chunk = connection.recv(256)
                        if not chunk:
                            break
                        data += chunk
                except OSError:
                    pass
                received.append(data)
                frame = f"${len(PASSWORD)}\r\n{PASSWORD}\r\n".encode()
                if b"AUTH" in data and frame in data:
                    connection.sendall(b"+OK\r\n+PONG\r\n")
                elif b"AUTH" in data:
                    connection.sendall(b"-WRONGPASS invalid username-password pair\r\n")
                else:
                    connection.sendall(b"-NOAUTH Authentication required.\r\n")

    threading.Thread(target=serve, daemon=True).start()
    yield port, received
    server.close()


def test_probe_authenticates_before_ping(requirepass_server: tuple[int, list[bytes]]) -> None:
    port, received = requirepass_server

    assert probes.redis_ping("127.0.0.1", port, password=PASSWORD, timeout=1.0) is None
    assert received[0].startswith(b"*2\r\n$4\r\nAUTH\r\n")


def test_probe_without_password_reports_noauth(
    requirepass_server: tuple[int, list[bytes]],
) -> None:
    port, _ = requirepass_server

    error = probes.redis_ping("127.0.0.1", port, timeout=1.0)

    assert error is not None
    assert "NOAUTH" in error


def test_probe_never_echoes_a_wrong_password(
    requirepass_server: tuple[int, list[bytes]],
) -> None:
    port, _ = requirepass_server

    error = probes.redis_ping("127.0.0.1", port, password="not-the-secret", timeout=1.0)

    assert error is not None
    assert "WRONGPASS" in error
    assert "not-the-secret" not in error


def test_service_checks_send_the_configured_falkordb_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_ping(host: str, port: int, **kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(probes, "http_ok", lambda *a, **k: None)
    monkeypatch.setattr(probes, "tcp_reachable", lambda *a, **k: None)
    monkeypatch.setattr(probes, "redis_ping", fake_ping)

    suite._service_checks(
        RuntimeSettings(falkordb_username="graph", falkordb_password=SecretStr("pw"))
    )

    assert seen["username"] == "graph"
    assert seen["password"] == "pw"
