"""The server-owned deadline every ``/v1`` handler runs under."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import Request

from harborrag_core.contracts.errors import HarborDeadlineExceeded

from .settings import ApiSettings


async def enforce_deadline(request: Request) -> AsyncIterator[None]:
    """Cancel the handler once it outlives ``api_request_timeout_seconds``.

    Declared with ``scope="function"`` so the deadline ends when the handler returns; a
    streamed body keeps its own, longer deadline.

    Raises:
        HarborDeadlineExceeded: The handler ran past the deadline.
    """
    settings: ApiSettings = request.app.state.settings
    try:
        async with asyncio.timeout(settings.api_request_timeout_seconds):
            yield
    except TimeoutError as exc:
        raise HarborDeadlineExceeded("API request exceeded its server deadline") from exc
