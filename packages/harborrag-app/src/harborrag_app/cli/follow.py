"""Follow one ingestion run from this process until it ends.

Handles:
- Reading the run through ``get_ingestion`` every ``POLL_SECONDS``; the server does nothing extra
- Table mode: one line whenever the run's status or counters change; JSON and YAML print only
  the final run, which the caller prints
- The CLI's held-back log records, printed when following starts and live from then on
- Ctrl-C: stop following, leave the run going and say how to re-attach

Does NOT pause, cancel or otherwise change the run, and sets no time limit; Ctrl-C ends it.

Start from: follow_ingestion, followed.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable

from harborrag_app.api.v1.admin.connection_schemas import IngestionOut
from harborrag_app.cli.exit_codes import RUN_FAILED
from harborrag_app.cli.invoke import Outcome, release_held_logs
from harborrag_app.cli.output import OutputFormat
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
    """
    release_held_logs()
    try:
        return await _poll(operations, scope, ingestion_id, output=output, sleep=sleep)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(REATTACH.format(ingestion_id=ingestion_id), file=sys.stderr)
        raise KeyboardInterrupt from None


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


def _progress_line(run: IngestionOut) -> str:
    counters = run.counters
    return (
        f"{run.ingestion_id}  {run.status}  discovered={counters.discovered} "
        f"processed={counters.processed} skipped={counters.skipped} failed={counters.failed}"
    )
