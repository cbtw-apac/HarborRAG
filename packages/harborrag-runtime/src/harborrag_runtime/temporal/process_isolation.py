"""Subprocess isolation for CPU-intensive or native-extension activities."""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import multiprocessing
import pickle
import queue as _stdlib_queue
import threading
import weakref
from collections.abc import Callable
from typing import Any, cast

from temporalio import activity
from temporalio.exceptions import ApplicationError

logger = logging.getLogger("harborrag.runtime.temporal.process_isolation")

# Patched in unit tests to avoid spawning real subprocesses.
_SUBPROCESS_CONTEXT: str = "spawn"

# Bound concurrent interpreters so activity concurrency cannot exhaust host
# memory; keyed by running loop so each event loop gets its own ceiling.
_MAX_CONCURRENT_SUBPROCESSES: int = 4
_subprocess_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _subprocess_slots_for_running_loop() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _subprocess_slots.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_SUBPROCESSES)
        _subprocess_slots[loop] = semaphore
    return semaphore


class SubprocessCrashError(ApplicationError):
    """Raised when an isolated subprocess exits non-zero or raises an exception."""

    def __init__(self, message: str) -> None:
        super().__init__(message, type="SubprocessCrash", non_retryable=False)


class SubprocessSerializationError(SubprocessCrashError):
    """Raised when subprocess spawn fails due to argument serialization."""


class SubprocessResultSerializationError(SubprocessSerializationError):
    """Raised when a subprocess result cannot be pickled back to the parent."""


def _is_spawn_serialization_exception(error: BaseException) -> bool:
    return isinstance(error, (AttributeError, TypeError, pickle.PicklingError))


def _start_process(proc: Any) -> None:
    try:
        proc.start()
    except Exception as error:
        if _is_spawn_serialization_exception(error):
            raise SubprocessSerializationError(
                f"isolated subprocess serialization failed: {type(error).__name__}: {error}"
            ) from error
        raise


def _encode_exception(error: BaseException, *, fallback: BaseException | None = None) -> bytes:
    """Serialize an exception, degrading to a plain RuntimeError when needed."""
    try:
        return pickle.dumps(error)
    except Exception:
        return pickle.dumps(fallback or RuntimeError(repr(error)))


def _validate_heartbeat_timeout(heartbeat_interval: float, info: Any) -> None:
    if info.heartbeat_timeout is None or info.heartbeat_timeout.total_seconds() <= 0:
        return
    limit = info.heartbeat_timeout.total_seconds()
    if heartbeat_interval >= limit:
        raise ValueError(
            f"heartbeat interval ({heartbeat_interval}s) must be shorter than "
            f"heartbeat_timeout ({limit:.0f}s)"
        )


def _subprocess_worker(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    result_queue: Any,
    heartbeat_interval: float,
) -> None:
    """Top-level target executed in the isolated process.

    Sends ("alive", None) pulses on each heartbeat interval while fn runs.
    Sends ("done", pickled_result) when fn completes, ("error", pickled_exc)
    when fn raises, or ("result_error", pickled_exc) when fn succeeds but its
    result cannot be pickled back to the parent. Payloads are pre-serialized
    here because `Queue.put` pickles asynchronously on a feeder thread, so a
    pickling failure there would otherwise be silently dropped instead of
    reported.
    """
    outcome: Any = None
    error: BaseException | None = None
    done = threading.Event()

    def _run() -> None:
        nonlocal outcome, error
        try:
            outcome = fn(*args, **kwargs)
        except BaseException as exc:
            error = exc
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()

    while not done.wait(timeout=heartbeat_interval):
        try:
            result_queue.put_nowait(("alive", None))
        except Exception:
            pass

    if error is not None:
        result_queue.put(("error", _encode_exception(error)))
        return

    try:
        payload = pickle.dumps(outcome)
    except Exception as exc:
        message = f"isolated subprocess result serialization failed: {type(exc).__name__}: {exc}"
        result_queue.put(("result_error", _encode_exception(RuntimeError(message))))
        return

    result_queue.put(("done", payload))


async def run_in_isolated_subprocess[ResultT](
    fn: Callable[..., ResultT],
    /,
    *args: Any,
    heartbeat_interval: float = 30.0,
    heartbeat_detail: object = "running",
    **kwargs: Any,
) -> ResultT:
    """Run *fn* in an isolated subprocess with subprocess-driven heartbeating.

    Falls back to thread execution outside an activity context (e.g. in tests
    that do not run a full Temporal activity environment).
    """
    if not math.isfinite(heartbeat_interval) or heartbeat_interval <= 0:
        raise ValueError(
            f"heartbeat interval must be finite and positive, got {heartbeat_interval!r}"
        )

    if not activity.in_activity():
        result = await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(fn, *args, **kwargs)
        )
        return result

    _validate_heartbeat_timeout(heartbeat_interval, activity.info())

    async with _subprocess_slots_for_running_loop():
        ctx = cast(Any, multiprocessing.get_context(_SUBPROCESS_CONTEXT))
        mp_queue: multiprocessing.Queue = ctx.Queue()
        proc = ctx.Process(
            target=_subprocess_worker,
            args=(fn, args, kwargs, mp_queue, heartbeat_interval),
        )
        _start_process(proc)

        loop = asyncio.get_running_loop()
        # Allow 2× the interval for each alive signal before declaring a hung process.
        poll_timeout = heartbeat_interval * 2.0

        # Send initial heartbeat before entering subprocess polling loop
        activity.heartbeat(heartbeat_detail)

        try:
            while True:
                try:
                    message = await loop.run_in_executor(
                        None,
                        functools.partial(mp_queue.get, timeout=poll_timeout),
                    )
                    kind, payload = cast(tuple[str, object], message)
                except _stdlib_queue.Empty:
                    # No alive signal → stop heartbeating so heartbeat_timeout fires.
                    logger.warning(
                        "Isolated subprocess appears hung (no signal for %.0fs pid=%s); "
                        "suspending heartbeat to allow server timeout",
                        poll_timeout,
                        proc.pid,
                    )
                    await loop.run_in_executor(None, functools.partial(proc.join, poll_timeout))
                    raise SubprocessCrashError(
                        f"isolated subprocess (pid={proc.pid}) hung without producing output"
                    )

                if kind == "alive":
                    activity.heartbeat(heartbeat_detail)
                elif kind == "done":
                    await loop.run_in_executor(None, functools.partial(proc.join, 5))
                    return cast(ResultT, pickle.loads(cast(bytes, payload)))
                elif kind == "result_error":
                    await loop.run_in_executor(None, functools.partial(proc.join, 5))
                    exc = cast(BaseException, pickle.loads(cast(bytes, payload)))
                    raise SubprocessResultSerializationError(
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                else:
                    # kind == "error"
                    await loop.run_in_executor(None, functools.partial(proc.join, 5))
                    exc = cast(BaseException, pickle.loads(cast(bytes, payload)))
                    raise SubprocessCrashError(f"{type(exc).__name__}: {exc}") from exc

        except asyncio.CancelledError:
            if proc.is_alive():
                logger.info("Activity cancelled; killing isolated subprocess pid=%s", proc.pid)
                proc.kill()
                await asyncio.shield(loop.run_in_executor(None, functools.partial(proc.join, 5)))
            raise
