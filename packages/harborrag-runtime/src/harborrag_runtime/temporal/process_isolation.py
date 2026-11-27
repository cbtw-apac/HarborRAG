"""Subprocess isolation for CPU-intensive or native-extension activities.

No isolated subprocess outlives its activity. Logs name the process and its timings,
never the function's arguments or result.
"""

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
from multiprocessing.process import BaseProcess
from multiprocessing.queues import Queue
from typing import Final

from temporalio import activity
from temporalio.exceptions import ApplicationError

from harborrag_core.observability.log_events import log_event

logger = logging.getLogger("harborrag.runtime.temporal.process_isolation")

_SUBPROCESS_CONTEXT: Final = "spawn"
"""Start method of isolated subprocesses."""

_MAX_CONCURRENT_SUBPROCESSES: int = 4
"""Isolated subprocesses one event loop runs at once, so activities cannot exhaust memory."""
_subprocess_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)
"""Subprocess slots of each running event loop, so each loop gets its own ceiling."""

_DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0
"""Seconds between a subprocess's alive signals when the caller does not choose."""
_HUNG_AFTER_INTERVALS = 2.0
"""Heartbeat intervals without an alive signal after which a subprocess counts as hung."""
_JOIN_TIMEOUT_SECONDS = 5.0
"""How long to wait for a subprocess that reported its outcome, or was killed, to exit."""


def _subprocess_slots_for_running_loop() -> asyncio.Semaphore:
    """Return the running event loop's semaphore of subprocess slots."""
    loop = asyncio.get_running_loop()
    semaphore = _subprocess_slots.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_SUBPROCESSES)
        _subprocess_slots[loop] = semaphore
    return semaphore


class SubprocessCrashError(ApplicationError):
    """An isolated subprocess hung, exited non-zero or raised an exception.

    Temporal retries the activity.

    Args:
        message: What went wrong, naming the subprocess's error type.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, type="SubprocessCrash", non_retryable=False)


class SubprocessSerializationError(SubprocessCrashError):
    """An isolated subprocess could not start because its arguments do not pickle."""


class SubprocessResultSerializationError(SubprocessSerializationError):
    """An isolated subprocess's result could not be pickled back to the parent."""


def _is_spawn_serialization_exception(error: BaseException) -> bool:
    """Return whether a start failure came from pickling the subprocess's arguments."""
    return isinstance(error, (AttributeError, TypeError, pickle.PicklingError))


def _start_process(proc: BaseProcess) -> None:
    """Start the subprocess, raising an argument pickling failure as a serialization error."""
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


def _validate_heartbeat_timeout(heartbeat_interval: float, info: activity.Info) -> None:
    """Reject a heartbeat interval that is not shorter than the activity's heartbeat timeout."""
    if info.heartbeat_timeout is None or info.heartbeat_timeout.total_seconds() <= 0:
        return
    limit = info.heartbeat_timeout.total_seconds()
    if heartbeat_interval >= limit:
        raise ValueError(
            f"heartbeat interval ({heartbeat_interval}s) must be shorter than "
            f"heartbeat_timeout ({limit:.0f}s)"
        )


def _subprocess_worker(
    fn: Callable[..., object],
    args: tuple[object, ...],
    kwargs: dict[str, object],
    result_queue: Queue[tuple[str, bytes | None]],
    heartbeat_interval: float,
) -> None:
    """Run ``fn`` as the isolated process's target and report its outcome on the queue.

    Sends ("alive", None) pulses on each heartbeat interval while fn runs.
    Sends ("done", pickled_result) when fn completes, ("error", pickled_exc)
    when fn raises, or ("result_error", pickled_exc) when fn succeeds but its
    result cannot be pickled back to the parent. Payloads are pre-serialized
    here because ``Queue.put`` pickles asynchronously on a feeder thread, so a
    pickling failure there would otherwise be silently dropped instead of
    reported.
    """
    outcome: object = None
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
    *args: object,
    heartbeat_interval: float = _DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    heartbeat_detail: object = "running",
    **kwargs: object,
) -> ResultT:
    """Run ``fn`` in an isolated subprocess whose alive signals drive the heartbeat.

    Outside an activity, such as in tests without a Temporal activity environment,
    ``fn`` runs on a thread instead. A subprocess silent for two heartbeat intervals
    counts as hung. Heartbeating then stops, so the server's heartbeat timeout ends the attempt.

    Args:
        fn: Top-level function to run; it, its arguments and its result must pickle.
        *args: Positional arguments for ``fn``.
        heartbeat_interval: Seconds between alive signals; finite, positive and shorter
            than the activity's heartbeat timeout.
        heartbeat_detail: Detail sent with each activity heartbeat.
        **kwargs: Keyword arguments for ``fn``.

    Returns:
        What ``fn`` returned.

    Raises:
        SubprocessCrashError: The subprocess hung or raised, or its arguments or result
            did not pickle.
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
        ctx = multiprocessing.get_context(_SUBPROCESS_CONTEXT)
        mp_queue = ctx.Queue()
        proc = ctx.Process(
            target=_subprocess_worker,
            args=(fn, args, kwargs, mp_queue, heartbeat_interval),
        )
        _start_process(proc)

        loop = asyncio.get_running_loop()
        poll_timeout = heartbeat_interval * _HUNG_AFTER_INTERVALS
        activity.heartbeat(heartbeat_detail)

        try:
            while True:
                try:
                    kind, payload = await loop.run_in_executor(
                        None,
                        functools.partial(mp_queue.get, timeout=poll_timeout),
                    )
                except _stdlib_queue.Empty:
                    log_event(
                        logger,
                        "temporal.isolated_subprocess_hung",
                        level=logging.WARNING,
                        reason="no_alive_signal",
                        process_id=proc.pid,
                        silent_seconds=poll_timeout,
                    )
                    await loop.run_in_executor(None, functools.partial(proc.join, poll_timeout))
                    raise SubprocessCrashError(
                        f"isolated subprocess (pid={proc.pid}) hung without producing output"
                    )

                if kind == "alive":
                    activity.heartbeat(heartbeat_detail)
                elif kind == "done":
                    await _join(proc, loop)
                    returned: ResultT = pickle.loads(payload)
                    return returned
                elif kind == "result_error":
                    await _join(proc, loop)
                    unpicklable: BaseException = pickle.loads(payload)
                    raise SubprocessResultSerializationError(
                        f"{type(unpicklable).__name__}: {unpicklable}"
                    ) from unpicklable
                else:
                    await _join(proc, loop)
                    crash: BaseException = pickle.loads(payload)
                    raise SubprocessCrashError(f"{type(crash).__name__}: {crash}") from crash

        except asyncio.CancelledError:
            log_event(logger, "temporal.isolated_subprocess_cancelled", process_id=proc.pid)
            raise
        finally:
            await _kill_if_alive(proc, loop)


async def _join(proc: BaseProcess, loop: asyncio.AbstractEventLoop) -> None:
    """Wait for a subprocess that reported its outcome to exit, at most a few seconds."""
    await loop.run_in_executor(None, functools.partial(proc.join, _JOIN_TIMEOUT_SECONDS))


async def _kill_if_alive(
    proc: BaseProcess,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Kill the subprocess if it is still alive, whichever way the activity exited.

    A hung subprocess outlives its timed-out ``join``, and ``join`` returns either way,
    so whether the process ended is checked here rather than assumed.
    """
    if not proc.is_alive():
        return
    log_event(
        logger,
        "temporal.isolated_subprocess_killed",
        level=logging.WARNING,
        reason="outlived_activity",
        process_id=proc.pid,
    )
    proc.kill()
    await asyncio.shield(
        loop.run_in_executor(None, functools.partial(proc.join, _JOIN_TIMEOUT_SECONDS))
    )
