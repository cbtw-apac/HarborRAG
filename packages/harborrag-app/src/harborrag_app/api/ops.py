"""The ops port: Prometheus ``/metrics`` behind the scrape token, and nothing else.

Handles:
- Answering ``GET /metrics`` on ``HARBORRAG_OPS_PORT`` to ``Authorization: Bearer
  <HARBORRAG_METRICS_SCRAPE_TOKEN>``, with ``401`` for any other credential
- ``404`` for every other path on the ops port, so the API is not reachable there

Does NOT serve ``/metrics`` on the public port: there the path is an unknown route.
"""

from __future__ import annotations

import hmac

from prometheus_client.exposition import CONTENT_TYPE_PLAIN_0_0_4
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth.dependencies import UNAUTHENTICATED
from .metrics import ApiMetrics
from .middleware import TRACE_HEADER, trace_id_of
from .settings import ApiSettings

METRICS_PATH = "/metrics"


def _error(scope: Scope, status: int, code: str, message: str) -> JSONResponse:
    """The error envelope for a request answered on the ops port."""
    trace_id = trace_id_of(scope)
    headers = {TRACE_HEADER: trace_id}
    if status == 401:
        headers["WWW-Authenticate"] = 'Bearer realm="harborrag-metrics"'
    return JSONResponse(
        status_code=status,
        content={
            "error": {"code": code, "message": message, "details": None, "trace_id": trace_id}
        },
        headers=headers,
    )


def _authorized(scope: Scope, token: bytes | None) -> bool:
    """Whether the request carries the scrape token."""
    scheme, _, credential = Headers(scope=scope).get("authorization", "").partition(" ")
    if token is None or scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(credential.strip().encode(), token)


class OpsPortMiddleware:
    """Serve ``/metrics`` on the ops socket and keep the rest of the API off it."""

    def __init__(self, app: ASGIApp, *, settings: ApiSettings, metrics: ApiMetrics) -> None:
        self._app = app
        self._port = settings.ops_port
        secret = settings.metrics_scrape_token
        self._token = secret.get_secret_value().encode() if secret is not None else None
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        server = scope.get("server")
        on_ops_port = (
            scope["type"] == "http"
            and self._port is not None
            and server is not None
            and server[1] == self._port
        )
        if not on_ops_port:
            await self._app(scope, receive, send)
            return
        response: Response
        if scope["path"] != METRICS_PATH or scope["method"] not in {"GET", "HEAD"}:
            response = _error(scope, 404, "not_found", "Not Found")
        elif not _authorized(scope, self._token):
            response = _error(scope, 401, "unauthenticated", UNAUTHENTICATED)
        else:
            response = Response(
                content=self._metrics.render(),
                headers={"Content-Type": CONTENT_TYPE_PLAIN_0_0_4},
            )
        await response(scope, receive, send)
