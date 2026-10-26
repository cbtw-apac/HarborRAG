"""Migration headers for superseded public endpoints."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SUNSET = "Sun, 07 Feb 2027 00:00:00 GMT"


def replacement(path: str) -> str | None:
    exact = {
        "/v1/graph/traverse": "/v1/retrieval/graph/subgraphs",
    }
    if path in exact:
        return exact[path]
    prefixes = {
        "/v1/memory/sessions/": "/v1/chat/sessions/",
    }
    for old, new in prefixes.items():
        if path == old or path.startswith(old if old.endswith("/") else old + "/"):
            return new + path[len(old) :]
    return None


class DeprecatedEndpointsMiddleware:
    """Annotate aliases, including streaming responses, without buffering bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        target = replacement(scope.get("path", "")) if scope["type"] == "http" else None
        if target is None:
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Deprecation"] = "true"
                headers["Sunset"] = SUNSET
                headers["Link"] = f'<{target}>; rel="successor-version"'
            await send(message)

        await self.app(scope, receive, with_headers)
