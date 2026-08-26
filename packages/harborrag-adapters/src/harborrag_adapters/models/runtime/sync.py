from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Awaitable
from typing import TypeVar

R = TypeVar("R")


class AsyncLoopRunner:
    """Run asynchronous client operations on one reusable background event loop."""

    def __init__(self, *, thread_name: str) -> None:
        """Start the background event-loop thread and block until it is ready."""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._startup_error: BaseException | None = None
        self._ready = threading.Event()
        self._stopped = False
        self._state_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name=thread_name, daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._startup_error is not None:
            raise RuntimeError("could not start model client event loop") from self._startup_error

    def _run(self) -> None:
        """Run the event loop on the background thread until stopped, then drain it."""
        try:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            return

        # Signal readiness from inside the first loop turn. Publishing the loop
        # before ``run_forever`` starts leaves a narrow race where a submitted
        # callback is queued before the selector wakeup channel is active.
        loop.call_soon(self._ready.set)
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()

    def submit(self, awaitable: Awaitable[R]) -> concurrent.futures.Future[R]:
        """Schedule ``awaitable`` on the background loop and return its future."""
        with self._state_lock:
            if self._stopped:
                close = getattr(awaitable, "close", None)
                if close is not None:
                    close()
                raise RuntimeError("model client is closed")
            loop = self._loop
            if loop is None:
                raise RuntimeError("model client event loop did not start")

            async def resolve() -> R:
                """Await the submitted coroutine on the background event loop."""
                return await awaitable

            return asyncio.run_coroutine_threadsafe(resolve(), loop)

    def run(self, awaitable: Awaitable[R]) -> R:
        """Submit ``awaitable`` and block the calling thread for its result."""
        return self.submit(awaitable).result()

    def stop(self) -> None:
        """Stop the background event loop and join its thread."""
        with self._state_lock:
            if self._stopped:
                return
            self._stopped = True
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
        self._thread.join()


def run_awaitable_synchronously[R](awaitable: Awaitable[R], *, thread_name: str) -> R:
    """Resolve an awaitable from synchronous code, including inside a running loop."""

    async def resolve() -> R:
        return await awaitable

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(resolve())

    values: list[R] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            values.append(asyncio.run(resolve()))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, name=thread_name)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return values[0]
