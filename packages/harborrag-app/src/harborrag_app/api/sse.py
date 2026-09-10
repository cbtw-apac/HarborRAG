"""Server-Sent Events framing and the server-owned stream deadline."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator

logger = logging.getLogger("harborrag.app.api.sse")

STREAM_DEADLINE_ERROR_CODE = "harbor_deadline_exceeded"


def sse_frame(name: str, payload: object) -> bytes:
    """Encode one ``event: <name>\\ndata: <json>\\n\\n`` frame."""

    return f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()


async def bounded_sse_frames(
    frames: AsyncGenerator[bytes, None],
    *,
    timeout_seconds: float,
    error_message: str,
) -> AsyncIterator[bytes]:
    """Relay ``frames`` until they end or ``timeout_seconds`` of wall-clock elapse.

    HTTP status is fixed once the first frame is on the wire, so a stream that
    outlives its budget must end with exactly one in-band terminal ``error``
    frame rather than a truncated body. The timeout wraps each ``__anext__``
    individually (not the ``yield``): ``asyncio.timeout`` converts its
    cancellation into ``TimeoutError`` only when the cancellation lands inside
    the guarded block, and while this generator is suspended at ``yield`` the
    task is awaiting the transport's ``send`` instead. Time spent there still
    counts against the deadline through the remaining-time check.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            try:
                async with asyncio.timeout(remaining):
                    frame = await anext(frames)
            except StopAsyncIteration:
                return
            yield frame
    except TimeoutError:
        logger.warning("SSE stream exceeded its %.0fs server deadline", timeout_seconds)
        yield sse_frame(
            "error",
            {"code": STREAM_DEADLINE_ERROR_CODE, "message": error_message},
        )
    finally:
        await frames.aclose()


__all__ = ["STREAM_DEADLINE_ERROR_CODE", "bounded_sse_frames", "sse_frame"]
