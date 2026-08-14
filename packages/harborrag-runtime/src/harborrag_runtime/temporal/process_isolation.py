"""Subprocess isolation for CPU-intensive or native-extension activities."""

from __future__ import annotations

import asyncio
import functools
import logging
import multiprocessing
import pickle
import queue as _stdlib_queue
import threading
from collections.abc import Callable
from typing import Any, cast

from temporalio import activity
from temporalio.exceptions import ApplicationError

logger = logging.getLogger("harborrag.runtime.temporal.process_isolation")

# Patched in unit tests to avoid spawning real subprocesses.
_SUBPROCESS_CONTEXT: str = "spawn"


class SubprocessCrashError(ApplicationError):
    """Raised when an isolated subprocess exits non-zero or raises an exception."""

    def __init__(self, message: str) -> None:
        super().__init__(message, type="SubprocessCrash", non_retryable=False)


class SubprocessSerializationError(SubprocessCrashError):
    """Raised when subprocess spawn fails due to argument serialization."""


def _is_spawn_serialization_exception(error: BaseException) -> bool:
    return isinstance(error, (AttributeError, TypeError, pickle.PicklingError))


def _subprocess_worker(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    result_queue: Any,
    heartbeat_interval: float,
) -> None:
    """Top-level target executed in the isolated process.

    Sends ("alive", None) pulses on each heartbeat interval while fn runs.
    Sends ("done", result) or ("error", exc) when fn completes or raises.
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
        try:
            result_queue.put(("error", error))
        except Exception:
            result_queue.put(("error", RuntimeError(repr(error))))
    else:
        result_queue.put(("done", outcome))


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
    if not activity.in_activity():
        result = await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(fn, *args, **kwargs)
        )
        return cast(ResultT, result)

    ctx = cast(Any, multiprocessing.get_context(_SUBPROCESS_CONTEXT))
    mp_queue: multiprocessing.Queue = ctx.Queue()
    proc = ctx.Process(
        target=_subprocess_worker,
        args=(fn, args, kwargs, mp_queue, heartbeat_interval),
        daemon=True,
    )
    try:
        proc.start()
    except Exception as error:
        if _is_spawn_serialization_exception(error):
            raise SubprocessSerializationError(
                f"isolated subprocess serialization failed: {type(error).__name__}: {error}"
            ) from error
        raise

    loop = asyncio.get_running_loop()
    # Allow 2× the interval for each alive signal before declaring a hung process.
    poll_timeout = heartbeat_interval * 2.0

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
                await loop.run_in_executor(None, proc.join)
                raise SubprocessCrashError(
                    f"isolated subprocess (pid={proc.pid}) hung without producing output"
                )

            if kind == "alive":
                activity.heartbeat(heartbeat_detail)
            elif kind == "done":
                await loop.run_in_executor(None, functools.partial(proc.join, 5))
                return cast(ResultT, payload)
            else:
                # kind == "error"
                await loop.run_in_executor(None, functools.partial(proc.join, 5))
                message = f"{type(payload).__name__}: {payload}"
                if isinstance(payload, BaseException):
                    raise SubprocessCrashError(message) from payload
                raise SubprocessCrashError(message)

    except asyncio.CancelledError:
        if proc.is_alive():
            logger.info("Activity cancelled; killing isolated subprocess pid=%s", proc.pid)
            proc.kill()
            proc.join(timeout=5)
        raise
