"""Network probes for local services, standard library only.

Each probe returns ``None`` on success or a short error string, so the suite can turn it
into a check without importing a provider client that may not be installed.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 3.0
_HTTP_OK_UPPER_BOUND = 400


def tcp_reachable(host: str, port: int, *, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return f"{host}:{port} unreachable ({exc.strerror or exc})"


def safe_url(url: str) -> str:
    """Scheme, host, port and path only.

    A configured endpoint may embed userinfo or a token query parameter, and every error
    string below reaches ``Check.detail`` and the JSON doctor output.
    """

    try:
        parsed = urlparse(url)
    except ValueError:
        return "the configured URL"
    if not parsed.scheme or not parsed.hostname:
        return "the configured URL"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}{parsed.path}"


def http_ok(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> str | None:
    request = urllib.request.Request(url, method="GET")  # noqa: S310 - health URL from settings
    shown = safe_url(url)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        return f"{shown} returned HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"{shown} unreachable ({getattr(exc, 'reason', exc)})"
    if status < _HTTP_OK_UPPER_BOUND:
        return None
    return f"{shown} returned HTTP {status}"


_UNAUTHORIZED = (401, 403)
QDRANT_KEY_VARIABLE = "HARBORRAG_QDRANT_API_KEY"


def qdrant_authorized(
    base_url: str, api_key: str | None, *, timeout: float = DEFAULT_TIMEOUT
) -> str | None:
    """Probe an authenticated Qdrant route; ``/readyz`` answers without a key.

    The key is sent as an ``api-key`` header only and never appears in a result.
    """

    url = base_url.rstrip("/") + "/collections"
    headers = {"api-key": api_key} if api_key else {}
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    shown = safe_url(url)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        if exc.code in _UNAUTHORIZED:
            if api_key:
                return f"Qdrant rejected the API key (HTTP {exc.code}); check {QDRANT_KEY_VARIABLE}"
            return f"Qdrant requires an API key and none is configured; set {QDRANT_KEY_VARIABLE}"
        return f"{shown} returned HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"{shown} unreachable ({getattr(exc, 'reason', exc)})"
    if status < _HTTP_OK_UPPER_BOUND:
        return None
    return f"{shown} returned HTTP {status}"


def _resp(*parts: bytes) -> bytes:
    frame = b"*%d\r\n" % len(parts)
    for part in parts:
        frame += b"$%d\r\n%s\r\n" % (len(part), part)
    return frame


def redis_ping(
    host: str,
    port: int,
    *,
    username: str | None = None,
    password: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str | None:
    """RESP ``PING`` without a Redis client; FalkorDB speaks the same protocol.

    With a password the probe pipelines ``AUTH`` first, so a ``requirepass``
    server answers ``+OK`` then ``+PONG``. Replies never contain the password.
    """

    command = _resp(b"PING")
    expected = b"+PONG"
    if password:
        credentials = [password.encode("utf-8")]
        if username:
            credentials.insert(0, username.encode("utf-8"))
        command = _resp(b"AUTH", *credentials) + command
        expected = b"+OK\r\n+PONG"
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.sendall(command)
            reply = b""
            while (
                not reply.startswith(expected)
                and b"\r\n-" not in b"\r\n" + reply
                and len(reply) < 256
            ):
                chunk = connection.recv(64)
                if not chunk:
                    break
                reply += chunk
    except OSError as exc:
        return f"{host}:{port} unreachable ({exc.strerror or exc})"
    if reply.startswith(expected):
        return None
    return f"{host}:{port} answered {reply[:48]!r} instead of PONG"


def host_port(url: str, *, default_port: int) -> tuple[str, int]:
    parsed = urlparse(url)
    return parsed.hostname or "localhost", parsed.port or default_port


__all__ = [
    "DEFAULT_TIMEOUT",
    "host_port",
    "http_ok",
    "qdrant_authorized",
    "redis_ping",
    "safe_url",
    "tcp_reachable",
]
