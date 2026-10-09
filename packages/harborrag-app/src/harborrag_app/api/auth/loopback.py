"""Loopback-only admission for the unauthenticated development API.

With ``auth_mode=none`` every request is an implicit owner of every tenant, so
the process must serve only the local machine. ``HARBORRAG_HOST`` says nothing
about the socket uvicorn really bound (``uvicorn --host 0.0.0.0`` overrides
it), and a page using DNS rebinding reaches a loopback listener with a foreign
Host header. The middleware therefore checks every request:

- the Host header must name the loopback interface (always), and
- the peer and the local socket address must both be loopback, unless the
  operator set ``HARBORRAG_ALLOW_INSECURE_DEV=true`` for a container whose
  published port is itself bound to loopback.
"""

from __future__ import annotations

import re
from ipaddress import IPv6Address, ip_address

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from harborrag_app.api.settings import ApiSettings

_LOOPBACK_HOST_HEADER = re.compile(
    r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", re.IGNORECASE | re.ASCII
)
_MISDIRECTED = (
    421,
    "misdirected_request",
    "Unauthenticated development mode answers only requests addressed to localhost",
)
_NOT_LOOPBACK = (
    403,
    "loopback_required",
    "Unauthenticated development mode accepts only loopback connections",
)


def is_loopback_address(host: str) -> bool:
    """Accept explicit loopback names and addresses, never unresolved hostnames."""

    normalized = host.strip().lower().removeprefix("[").removesuffix("]")
    if normalized == "localhost":
        return True
    try:
        address = ip_address(normalized)
    except ValueError:
        return False
    # A dual-stack listener reports an IPv4 loopback peer as ::ffff:127.0.0.1,
    # which ipaddress does not call loopback.
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def is_loopback_host_header(value: str) -> bool:
    """Whether a Host header names localhost, 127.0.0.1 or [::1], port optional."""

    return _LOOPBACK_HOST_HEADER.fullmatch(value) is not None


def _socket_is_loopback(endpoint: object) -> bool:
    if not isinstance(endpoint, (tuple, list)) or not endpoint:
        return False
    host = endpoint[0]
    return isinstance(host, str) and is_loopback_address(host)


class LoopbackOnlyMiddleware:
    """Refuse requests that are not addressed to, from and on the loopback interface."""

    def __init__(self, app: ASGIApp, *, require_loopback_socket: bool) -> None:
        self._app = app
        self._require_loopback_socket = require_loopback_socket

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        refusal = self._refusal(scope)
        if refusal is None:
            await self._app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        status_code, code, message = refusal
        response = JSONResponse(
            status_code=status_code,
            content={"error": {"code": code, "message": message, "details": {}, "trace_id": None}},
        )
        await response(scope, receive, send)

    def _refusal(self, scope: Scope) -> tuple[int, str, str] | None:
        hosts = Headers(scope=scope).getlist("host")
        if len(hosts) != 1 or not is_loopback_host_header(hosts[0]):
            return _MISDIRECTED
        if self._require_loopback_socket and not (
            _socket_is_loopback(scope.get("client")) and _socket_is_loopback(scope.get("server"))
        ):
            return _NOT_LOOPBACK
        return None


def install_loopback_guard(app: Starlette, settings: ApiSettings) -> None:
    """With authentication off, admit only loopback traffic (call after other middleware)."""

    if settings.auth_mode == "none":
        app.add_middleware(
            LoopbackOnlyMiddleware,
            require_loopback_socket=not settings.allow_insecure_dev,
        )
