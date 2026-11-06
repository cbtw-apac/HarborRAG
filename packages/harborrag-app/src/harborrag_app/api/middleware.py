"""Trace ids and the request body limit.

Handles:
- One 32-hex trace id per request, from a valid ``X-Trace-Id`` or ``traceparent`` or newly
  minted, stored on ``request.state.trace_id`` and echoed as ``X-Trace-Id``
- Refusing a body over the limit with ``413 payload_too_large`` before it is parsed
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from uuid import uuid4

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

TRACE_HEADER = "X-Trace-Id"
_VALID_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")
_ZERO_TRACE_ID = "0" * 32


def _valid(trace_id: str | None) -> str | None:
    """Return ``trace_id`` when it is 32 lowercase hex characters and not all zeros."""
    if trace_id is not None and _VALID_TRACE_ID.fullmatch(trace_id) and trace_id != _ZERO_TRACE_ID:
        return trace_id
    return None


def _trace_id(headers: Headers) -> str:
    """Pick the caller's trace id, else the W3C ``traceparent``'s, else a new one."""
    supplied = _valid(headers.get(TRACE_HEADER))
    if supplied is not None:
        return supplied
    parent = _TRACEPARENT.fullmatch(headers.get("traceparent", "").strip())
    return (_valid(parent.group(1)) if parent else None) or uuid4().hex


def trace_id_of(scope: Scope) -> str:
    """Return the trace id stored in ``scope``, minting one when none is set yet."""
    state = scope.setdefault("state", {})
    trace_id = state.get("trace_id")
    if not isinstance(trace_id, str):
        trace_id = _trace_id(Headers(scope=scope))
        state["trace_id"] = trace_id
    return trace_id


def current_trace_id(request: Request) -> str:
    """Return the request's trace id, minting one when no middleware set it."""
    trace_id = getattr(request.state, "trace_id", None)
    if not isinstance(trace_id, str):
        trace_id = _trace_id(request.headers)
        request.state.trace_id = trace_id
    return trace_id


class TraceIdMiddleware(BaseHTTPMiddleware):
    """Attach a trace id to every request and echo it on the response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Populate request.state.trace_id and mirror it into the response."""
        trace_id = _trace_id(request.headers)
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers[TRACE_HEADER] = trace_id
        return response


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP bodies before request parsing or route execution."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        content_length = Headers(scope=scope).get("content-length")
        if content_length is not None and _oversized_content_length(
            content_length,
            limit=self._max_body_bytes,
        ):
            await _request_too_large(scope, send)
            return

        messages = await _bounded_request_messages(
            receive,
            max_body_bytes=self._max_body_bytes,
        )
        if messages is None:
            await _request_too_large(scope, send)
            return

        next_message = 0

        async def replay_receive() -> Message:
            nonlocal next_message
            if next_message < len(messages):
                message = messages[next_message]
                next_message += 1
                return message
            return await receive()

        await self._app(scope, replay_receive, send)


def _oversized_content_length(value: str, *, limit: int) -> bool:
    try:
        return int(value) > limit
    except ValueError:
        return False


async def _bounded_request_messages(
    receive: Receive,
    *,
    max_body_bytes: int,
) -> Sequence[Message] | None:
    messages: list[Message] = []
    body_bytes = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            return messages
        body_bytes += len(message.get("body", b""))
        if body_bytes > max_body_bytes:
            return None
        if not message.get("more_body", False):
            return messages


async def _request_too_large(scope: Scope, send: Send) -> None:
    trace_id = trace_id_of(scope)
    response = JSONResponse(
        status_code=413,
        content={
            "error": {
                "code": "payload_too_large",
                "message": "Request body exceeds the configured limit",
                "details": None,
                "trace_id": trace_id,
            }
        },
        headers={TRACE_HEADER: trace_id},
    )
    await response(scope, _empty_receive, send)


async def _empty_receive() -> Message:
    return {"type": "http.disconnect"}
