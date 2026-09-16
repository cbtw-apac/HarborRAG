"""Time budgets spanning structured model routing and validation repairs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from harborrag_adapters.models.runtime.operation_deadline import (
    OperationDeadlineExceeded,
    operation_deadline,
    remaining_timeout,
)
from harborrag_core.models.chat import HarborChatRequest
from harborrag_core.models.errors import HarborChatTimeoutError


def _timeout(request: HarborChatRequest) -> HarborChatTimeoutError:
    return HarborChatTimeoutError(
        "structured chat operation deadline exceeded",
        operation="chat_structured",
        logical_model=request.logical_model,
        request_id=request.metadata.request_id,
        retryable=False,
    )


@contextmanager
def structured_deadline(seconds: float | None, request: HarborChatRequest) -> Iterator[None]:
    """Sync deadlines rely on provider/transport timeouts and boundary checks."""

    try:
        with operation_deadline(seconds):
            yield
    except OperationDeadlineExceeded as exc:
        raise _timeout(request) from exc


@asynccontextmanager
async def async_structured_deadline(
    seconds: float | None, request: HarborChatRequest
) -> AsyncIterator[None]:
    """Cancel all awaited work, including admission and retry/repair loops."""

    with structured_deadline(seconds, request):
        timeout = asyncio.timeout(remaining_timeout())
        try:
            async with timeout:
                yield
        except TimeoutError as exc:
            if timeout.expired():
                raise _timeout(request) from exc
            raise
