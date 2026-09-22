"""Bounded in-process queue for best-effort long-term memory extraction.

Extraction costs a model call, so it must not sit in the request path: the
answer is already paid for and returned by the time an exchange is worth
remembering. The obvious durable alternative is not one -- the
``pending_control_plane_effects`` table is a *failed-effect retry* queue
drained by a recovery lease, so putting first-attempt work there would make
every extraction a "failure" replayed by whichever process holds the lease.

So this is an in-process bounded queue with a fixed worker pool, started and
drained by the API lifespan. The semantics are deliberately best-effort:

* ``submit`` never raises and never blocks. A full queue drops the exchange
  with a WARNING (identifiers only) and bumps :func:`dropped_extractions`;
  the turn is already persisted in conversation history, so the loss is one
  set of derived facts, not the conversation.
* Each worker catches everything per item and bounds it with a timeout, so a
  hanging model call cannot wedge the pool or block shutdown.
* ``drain`` finishes what is already queued, then stops the workers. It is
  idempotent and safe when ``start`` was never called -- the CLI path runs
  with no queue at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from .context import memory_context_request

if TYPE_CHECKING:
    from harborrag_core.ports.conversation import ConversationMessage
    from harborrag_core.ports.memory import MemoryIndex, MemoryRepository
    from harborrag_runtime.memory import MemoryContextRequest
    from harborrag_runtime.sdk import HarborRAG

    from .identity import MemoryIdentity

type RuntimeProvider = Callable[[], "HarborRAG"]
type _Item = tuple["MemoryContextRequest", tuple["ConversationMessage", ...]]

logger = logging.getLogger("harborrag.app.workflow_control.memory.extraction")

_DEFAULT_ITEM_TIMEOUT_SECONDS = 60.0
_dropped = 0


def dropped_extractions() -> int:
    """How many exchanges this process dropped because the queue was full."""

    return _dropped


def reset_dropped_extractions() -> None:
    """Reset the drop counter (tests and long-lived process introspection)."""

    global _dropped
    _dropped = 0


class MemoryExtractionQueue:
    """Run memory extraction for finished exchanges off the request path."""

    def __init__(  # noqa: PLR0913 - one keyword-only collaborator per injected port
        self,
        *,
        runtime_provider: RuntimeProvider,
        memories: MemoryRepository | None,
        index: MemoryIndex | None = None,
        max_queue: int = 256,
        workers: int = 2,
        item_timeout_seconds: float = _DEFAULT_ITEM_TIMEOUT_SECONDS,
    ) -> None:
        if max_queue < 1 or workers < 1:
            raise ValueError("memory extraction queue needs a positive size and worker count")
        self._runtime_provider = runtime_provider
        self._memories = memories
        self._index = index
        self._worker_count = workers
        self._item_timeout_seconds = item_timeout_seconds
        self._queue: asyncio.Queue[_Item] = asyncio.Queue(maxsize=max_queue)
        self._workers: list[asyncio.Task[None]] = []
        # ``_started`` is the contract with callers ("is anything going to run
        # this?"); ``_workers`` is the pool itself, which ``drain`` empties
        # while the tasks are still finishing. They are not the same question.
        self._started = False
        self._closing = False

    def submit(
        self,
        request: MemoryContextRequest,
        messages: Sequence[ConversationMessage],
    ) -> bool:
        """Enqueue one finished exchange; ``False`` when it was dropped.

        Never raises: a caller that just answered a question must not fail
        because the extraction backlog is full or the queue is shutting down.
        A queue whose pool was never started -- the CLI path -- refuses
        outright rather than accumulating work nothing will ever run.
        """

        global _dropped
        if self._memories is None or not messages or self._closing or not self._started:
            return False
        try:
            self._queue.put_nowait((request, tuple(messages)))
        except asyncio.QueueFull:
            _dropped += 1
            logger.warning(
                "Memory extraction queue is full; dropping the exchange for tenant=%s "
                "session=%s (dropped_total=%d)",
                request.tenant_id,
                request.session_id,
                _dropped,
            )
            return False
        return True

    async def start(self) -> None:
        """Start the worker pool; a second call is a no-op."""

        if self._started:
            return
        self._started = True
        self._closing = False
        self._workers = [
            asyncio.create_task(self._worker(), name=f"memory-extraction-{index}")
            for index in range(self._worker_count)
        ]

    async def drain(self, *, timeout: float = 10.0) -> None:
        """Finish queued work within ``timeout``, then stop the workers.

        Idempotent, and a no-op when the pool was never started.
        """

        self._closing = True
        if not self._workers:
            self._started = False
            return
        workers, self._workers = self._workers, []
        try:
            async with asyncio.timeout(timeout):
                await self._queue.join()
        except TimeoutError:
            logger.warning(
                "Memory extraction did not drain within %.0fs; %d exchanges were abandoned",
                timeout,
                self._queue.qsize(),
            )
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            self._started = False

    async def _worker(self) -> None:
        while True:
            request, messages = await self._queue.get()
            try:
                await self._extract(request, messages)
            finally:
                self._queue.task_done()

    async def _extract(
        self,
        request: MemoryContextRequest,
        messages: tuple[ConversationMessage, ...],
    ) -> None:
        """Run one extraction, absorbing every failure but cancellation."""

        if self._memories is None:
            return
        try:
            async with asyncio.timeout(self._item_timeout_seconds):
                await self._runtime_provider().memory.extract(
                    request,
                    messages=messages,
                    memories=self._memories,
                    index=self._index,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad exchange must not kill the pool
            logger.warning(
                "Memory extraction failed for tenant=%s session=%s; the turn stays in history",
                request.tenant_id,
                request.session_id,
                exc_info=True,
            )


def submit_exchange(
    queue: MemoryExtractionQueue | None,
    identity: MemoryIdentity,
    query: str,
    messages: Sequence[ConversationMessage] | None,
) -> None:
    """Queue one persisted exchange for extraction, when a queue is wired.

    ``messages`` being ``None`` means the turn was not written to conversation
    history, and nothing is submitted: extracted memories cite the history
    message ids as provenance, so facts must never outlive their messages.
    """

    if queue is None or messages is None:
        return
    queue.submit(memory_context_request(identity, query), messages)


async def drain_extraction_queue(
    queue: MemoryExtractionQueue | None,
    *,
    timeout: float = 10.0,
) -> None:
    """Drain ``queue`` when one is wired, tolerating a shutdown-time failure."""

    if queue is None:
        return
    with contextlib.suppress(Exception):
        await queue.drain(timeout=timeout)


__all__ = [
    "MemoryExtractionQueue",
    "drain_extraction_queue",
    "dropped_extractions",
    "reset_dropped_extractions",
    "submit_exchange",
]
