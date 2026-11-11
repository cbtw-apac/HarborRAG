"""Follow one ingestion run from this process until it ends.

Handles:
- Reading the run through ``get_ingestion`` every ``POLL_SECONDS``; the server does nothing extra
- Table mode: one line whenever the run's status or counters change; JSON and YAML print only
  the final run, which the caller prints
- The CLI's held-back log records, printed when following starts and live from then on
- Ctrl-C: stop following, leave the run going and say how to re-attach
- A read that fails, other than a run not found: the read's error, worded to say the run keeps
  going, with its ``ingestion_id`` and how to re-attach, so a started run is never lost

Does NOT pause, cancel or otherwise change the run, and sets no time limit; Ctrl-C ends it.

Start from: follow_ingestion, followed.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from http import HTTPStatus

from harborrag_app.api.v1.admin.connection_schemas import IngestionOut
from harborrag_app.cli.exit_codes import RUN_FAILED
from harborrag_app.cli.invoke import Outcome, ReportedError, release_held_logs
from harborrag_app.cli.output import OutputFormat
from harborrag_app.operations.errors import ErrorBody, error_body
from harborrag_app.operations.port import Operations
from harborrag_app.operations.scope import Scope
from harborrag_app.workflow_control.control_plane.connection_views import ingestion_status
from harborrag_app.workflow_control.ingestion.presenters import TERMINAL_STATES

POLL_SECONDS = 5.0
ENDED_STATUSES = frozenset(ingestion_status(state) for state in TERMINAL_STATES)
REATTACH = (
    "Stopped following; the run keeps going. Re-attach with: "
    "harborrag ingestions get {ingestion_id} --watch"
)
READ_FAILED = (
    "{message}; stopped following, the run started and keeps going. Re-attach with: "
    "harborrag ingestions get {ingestion_id} --watch"
)


async def follow_ingestion(
    operations: Operations,
    scope: Scope,
    ingestion_id: str,
    *,
    output: OutputFormat,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> IngestionOut:
    """Read a run until it has succeeded, failed or been cancelled.

    In table mode a line ``<ingestion_id>  <status>  discovered=<n> processed=<n>
    skipped=<n> failed=<n>`` is printed whenever the status or a counter changes.

    Args:
        operations: Operations to read the run with.
        scope: Caller and tenant the run belongs to.
        ingestion_id: The run to follow.
        output: Format the command prints in; only table mode prints progress lines.
        sleep: Waits between two reads.

    Returns:
        The run as it ended.

    Raises:
        KeyboardInterrupt: Ctrl-C; the run keeps going and stderr says how to re-attach.
        ReportedError: A read failed, other than with the run not found; the error keeps
            the read's status and code, and names the run and how to re-attach.
        IngestionNotFoundError: No run has this id; raised as the read raised it.
    """
    release_held_logs()
    try:
        return await _poll(operations, scope, ingestion_id, output=output, sleep=sleep)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(REATTACH.format(ingestion_id=ingestion_id), file=sys.stderr)
        raise KeyboardInterrupt from None
    except Exception as exc:
        error = error_body(exc)
        if error.status == HTTPStatus.NOT_FOUND:
            raise
        raise ReportedError(_read_failed(error, ingestion_id)) from exc


def followed(run: IngestionOut, *, output: OutputFormat) -> Outcome:
    """The command's outcome for a run it followed: exit ``0`` when it succeeded, else ``8``.

    A run that succeeded with failed items still exits ``0``. Table mode has shown the run
    in its progress lines, so only JSON and YAML print it.
    """
    return Outcome(
        None if output is OutputFormat.TABLE else run,
        0 if run.status == "succeeded" else RUN_FAILED,
    )


async def _poll(
    operations: Operations,
    scope: Scope,
    ingestion_id: str,
    *,
    output: OutputFormat,
    sleep: Callable[[float], Awaitable[None]],
) -> IngestionOut:
    """Read the run every ``POLL_SECONDS``, printing each changed line, until it ends."""
    shown: str | None = None
    while True:
        run = await operations.get_ingestion(scope, ingestion_id)
        line = _progress_line(run)
        if output is OutputFormat.TABLE and line != shown:
            print(line, flush=True)
            shown = line
        if run.status in ENDED_STATUSES:
            return run
        await sleep(POLL_SECONDS)


def _read_failed(error: ErrorBody, ingestion_id: str) -> ErrorBody:
    """``error`` with the run's id in its details and how to re-attach in its message."""
    details = error.details
    if isinstance(details, Mapping):
        details = {**details, "ingestion_id": ingestion_id}
    elif isinstance(details, list):
        details = [*details, {"ingestion_id": ingestion_id}]
    else:
        details = {"ingestion_id": ingestion_id}
    message = READ_FAILED.format(message=error.message, ingestion_id=ingestion_id)
    return replace(error, message=message, details=details)


def _progress_line(run: IngestionOut) -> str:
    counters = run.counters
    return (
        f"{run.ingestion_id}  {run.status}  discovered={counters.discovered} "
        f"processed={counters.processed} skipped={counters.skipped} failed={counters.failed}"
    )
