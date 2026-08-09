"""Request-scoped trace-id middleware (ST2).

Reads X-Request-Id (or mints a UUID), stores it on request.state.trace_id for
the error envelope and HarborEvent.trace_id, and echoes it back as a response
header so clients and logs can correlate.
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

TRACE_HEADER = "X-Request-Id"
_VALID_TRACE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _trace_id(request: Request) -> str:
    supplied = request.headers.get(TRACE_HEADER)
    if supplied is not None and _VALID_TRACE_ID.fullmatch(supplied):
        return supplied
    return uuid4().hex


class TraceIdMiddleware(BaseHTTPMiddleware):
    """Attach a trace id to every request and echo it on the response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Populate request.state.trace_id and mirror it into the response."""
        trace_id = _trace_id(request)
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
    state = scope.get("state") or {}
    response = JSONResponse(
        status_code=413,
        content={
            "error": {
                "code": "request_too_large",
                "message": "Request body exceeds the configured limit",
                "details": {},
                "trace_id": state.get("trace_id"),
            }
        },
    )
    await response(scope, _empty_receive, send)


async def _empty_receive() -> Message:
    return {"type": "http.disconnect"}
