"""SSE delivery of a finished completion.

TODO: stream the answer as it is generated, with the Story 5 event names (Task 7).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

from fastapi.responses import StreamingResponse

from harborrag_app.api.sse import sse_frame


def completion_stream(body: str, *, replayed: bool) -> StreamingResponse:
    """Send a completion body as ``response.started``, one delta, then ``response.completed``.

    Args:
        body: The completion JSON, as returned and stored.
        replayed: Whether ``body`` was replayed for an idempotent retry.

    Returns:
        The ``text/event-stream`` response.
    """
    completion = json.loads(body)

    async def frames() -> AsyncGenerator[bytes, None]:
        yield sse_frame(
            "response.started",
            {
                "session_id": completion["session_id"],
                "mode": completion["mode"],
                "replayed": replayed,
            },
        )
        yield sse_frame("response.output_text.delta", {"content": completion["answer"]})
        yield sse_frame("response.completed", completion)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Idempotency-Replayed": str(replayed).lower(),
        },
    )


__all__ = ["completion_stream"]
