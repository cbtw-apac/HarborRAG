"""Request-scoped trace-id middleware (ST2).

Reads X-Request-Id (or mints a UUID), stores it on request.state.trace_id for
the error envelope and HarborEvent.trace_id, and echoes it back as a response
header so clients and logs can correlate.
"""

from __future__ import annotations

from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

TRACE_HEADER = "X-Request-Id"


class TraceIdMiddleware(BaseHTTPMiddleware):
    """Attach a trace id to every request and echo it on the response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Populate request.state.trace_id and mirror it into the response."""
        trace_id = request.headers.get(TRACE_HEADER) or uuid4().hex
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers[TRACE_HEADER] = trace_id
        return response
