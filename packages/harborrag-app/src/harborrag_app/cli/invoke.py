"""Run one operation for a CLI command and report it as the HTTP route would.

Handles:
- One application service per invocation: built off the event loop, refused when its control
  plane is not ready, closed afterwards
- Before a write: the checkout's control-database refusal and the new-tenant question
- The body on stdout and exit 0, or the exit code an ``Outcome`` names (``8`` for a followed
  run that failed); a result that cannot be printed after the operation ran is reported as
  that, exit 1, never as the operation failing; any failure as the HTTP error envelope or one
  line on stderr, with a trace id minted per invocation, and the spec's exit code
- A failure to set up the local service (settings, composition, control-plane readiness) as
  the operator's own configuration error: ``error_body``'s status and code, with a message
  that says what to fix instead of the opaque one an operation's failure gets
- The in-process server's log: unless ``HARBORRAG_LOG_LEVEL`` is set, what it would print is
  held back and printed only after a success, so a failure's stderr is its error report alone;
  a command that keeps working after its operation succeeded releases it early

Does NOT parse arguments or hold business rules; commands parse, operations decide.

Start from: run_operation, Outcome, release_held_logs.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, NoReturn, cast
from uuid import uuid4

import click
import typer
from pydantic import ValidationError

from harborrag_app.cli.environment import refuse_checkout_write
from harborrag_app.cli.exit_codes import INTERRUPTED, exit_code_for
from harborrag_app.cli.globals import Globals
from harborrag_app.cli.local_mode import cli_scope
from harborrag_app.cli.output import print_body, print_error
from harborrag_app.cli.tenant_guard import confirm_tenant_write
from harborrag_app.operations.errors import ErrorBody, error_body
from harborrag_app.operations.local import LocalOperations
from harborrag_app.operations.results import Accepted, Saved, Tagged
from harborrag_app.operations.scope import Scope
from harborrag_app.workflow_control import AppResponse, BaseAppService
from harborrag_app.workflow_control.composition.selection import runtime_app_service
from harborrag_app.workflow_control.errors import validation_error_detail
from harborrag_core.contracts.errors import HarborConfigurationError, HarborUnavailableError
from harborrag_core.observability.process_logging import (
    LEVEL_ENV_VAR,
    ROOT_LOGGER_NAME,
    _ManagedStreamHandler,
)

if TYPE_CHECKING:
    from harborrag_app.workflow_control.composition.service import AppService

type OperationCall = Callable[[LocalOperations, Scope], Awaitable[object]]

logger = logging.getLogger("harborrag.app.cli.invoke")

CONTROL_PLANE_NOT_READY = (
    "Control plane is not ready. Refusing to run against a database whose schema may be "
    "stale; run 'harborrag doctor' for diagnostics."
)
RESULT_NOT_PRINTED = "The operation succeeded, but its result could not be printed"
DEBUG_HINT = " (set HARBORRAG_LOG_LEVEL=DEBUG for details)"
_CLICK_OUTCOMES = (click.ClickException, click.exceptions.Exit, click.exceptions.Abort)


@dataclass(frozen=True, slots=True)
class Outcome:
    """A command's result whose exit code depends on it, such as a followed run that failed.

    Args:
        body: Printed as the operation's body; ``None`` when the command has shown it already.
        exit_code: Code to exit with once ``body`` is printed.
    """

    body: object
    exit_code: int


def release_held_logs() -> None:
    """Print the log records held back so far, and let later ones through as they happen.

    For a command that keeps working after its operation succeeded, such as following a run:
    its warnings show while it runs instead of at the end. Outside ``run_operation``, or with
    ``HARBORRAG_LOG_LEVEL`` set, nothing is held and this does nothing.
    """
    hold = _HOLD.get()
    if hold is not None:
        hold.release()


class LocalSetupError(Exception):
    """The local application service could not be built or is not ready to use.

    Args:
        cause: What failed: settings validation, composition or the readiness check.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(type(cause).__name__)
        self.cause = cause


def run_operation(
    operation_id: str | None, call: OperationCall, *, globals: Globals, write: bool = False
) -> NoReturn:
    """Run one operation, print its body or its error, and exit with the spec's code.

    Args:
        operation_id: Key of ``OPERATIONS``, which picks the response model the body prints
            with; ``None`` for a CLI-only command, whose body prints as it is.
        call: Calls the operation with the local operations and the CLI's scope; an
            ``Outcome`` it returns names the exit code.
        globals: The invocation's resolved global options.
        write: The operation writes, so the checkout refusal and the new-tenant question run.

    Raises:
        typer.Exit: Always: ``0`` after the body, or the code an ``Outcome`` names; else the
            code of the failure's status; ``130`` on Ctrl-C.
        click.UsageError: A write into a tenant with no data, without a terminal or ``--yes``.
        click.exceptions.Abort: The user declined that write at the question; Click exits 1.
    """
    trace_id = uuid4().hex
    if write:
        refuse_checkout_write()
    with _held_logs():
        try:
            result = asyncio.run(_run(call, globals=globals, write=write))
        except KeyboardInterrupt:
            raise typer.Exit(INTERRUPTED) from None
        except _CLICK_OUTCOMES:
            raise
        except Exception as exc:  # noqa: BLE001 - the CLI's one error boundary, as HTTP's handlers
            error = setup_error(exc.cause) if isinstance(exc, LocalSetupError) else error_body(exc)
            logger.debug("%s failed trace_id=%s", operation_id, trace_id, exc_info=exc)
            print_error(error, output=globals.output, trace_id=trace_id)
            raise typer.Exit(exit_code_for(error.status)) from None
        if not (isinstance(result, Outcome) and result.body is None):
            _print_result(operation_id, result, globals=globals, trace_id=trace_id)
    raise typer.Exit(result.exit_code if isinstance(result, Outcome) else 0)


def _print_result(
    operation_id: str | None, result: object, *, globals: Globals, trace_id: str
) -> None:
    """Print a successful result; a failure here is reported as the print failing.

    The operation has already run, and a write is committed, so the error says so instead of
    reporting the operation as failed.

    Raises:
        typer.Exit: ``1`` when the result could not be printed.
    """
    try:
        print_body(operation_id, _unwrapped(result), output=globals.output)
    except Exception as exc:  # noqa: BLE001 - after the operation: report the print, not the work
        logger.debug("%s result not printed trace_id=%s", operation_id, trace_id, exc_info=exc)
        error = ErrorBody(500, "internal_error", RESULT_NOT_PRINTED, None)
        print_error(error, output=globals.output, trace_id=trace_id)
        raise typer.Exit(exit_code_for(error.status)) from None


@asynccontextmanager
async def local_operations() -> AsyncIterator[LocalOperations]:
    """Build the application service, and close it once the caller is done.

    Yields:
        The operations, run in this process over that service.

    Raises:
        LocalSetupError: Settings, composition or the control-plane readiness check failed;
            its ``cause`` is a ``HarborUnavailableError`` when the control plane is not ready.
    """
    try:
        service = await build_service(runtime_app_service)
    except Exception as exc:
        raise LocalSetupError(exc) from exc
    try:
        if control_plane_failure(service) is not None:
            raise LocalSetupError(HarborUnavailableError(CONTROL_PLANE_NOT_READY))
        yield LocalOperations(cast("AppService", service))  # runtime composition builds one
    finally:
        await close(service)


def setup_error(cause: Exception) -> ErrorBody:
    """Map a failed local setup to ``error_body``'s status and code, with an actionable message.

    The setup reads only the operator's own configuration, so its message can say what to
    fix: a settings ``ValidationError`` as ``loc: msg`` pairs without input values, a
    ``HarborConfigurationError`` as its message, anything else as the usual message plus a
    hint to rerun with ``HARBORRAG_LOG_LEVEL=DEBUG``.

    Args:
        cause: The exception that stopped the setup.

    Returns:
        The error to print.
    """
    body = error_body(cause)
    if isinstance(cause, ValidationError):
        return replace(body, message=validation_error_detail(cause))
    if isinstance(cause, HarborConfigurationError):
        return replace(body, message=str(cause) or type(cause).__name__)
    return replace(body, message=f"{body.message}{DEBUG_HINT}")


async def build_service(factory: Callable[[], BaseAppService]) -> BaseAppService:
    """Compose the application service in a worker thread.

    Composition runs migrations and a database probe that drive their own event loops.

    Args:
        factory: Builds the service; the commands pass ``runtime_app_service``.

    Returns:
        The composed service; the caller closes it with ``close``.
    """
    logger.debug("Building the runtime application service")
    return await asyncio.to_thread(factory)


def control_plane_failure(service: BaseAppService) -> AppResponse | None:
    """Return a failure response when the control plane is unusable, else ``None``.

    Composition boots degraded when migrations fail, so a command would otherwise fail
    late with an opaque error such as a missing column. A service that cannot report its
    health is not evidence of a broken control plane, so that counts as ready.
    """
    try:
        health = service.health()
    except Exception:
        logger.debug("Control-plane readiness check unavailable", exc_info=True)
        return None
    if health.ok:
        return None
    logger.error("Control-plane readiness check failed: %s", _control_db_error(health))
    return AppResponse(
        False, data={"error_type": "ControlPlaneUnavailable"}, error=CONTROL_PLANE_NOT_READY
    )


async def close(service: BaseAppService) -> None:
    """Close the service; a failure is logged, never raised over the printed result."""
    aclose = getattr(service, "aclose", None)
    if aclose is None:
        return
    try:
        result = aclose()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.warning(
            "Closing the application service failed", exc_info=logger.isEnabledFor(logging.DEBUG)
        )


async def _run(call: OperationCall, *, globals: Globals, write: bool) -> object:
    """Open the operations, ask about a new tenant before a write, and call the operation."""
    async with local_operations() as operations:
        if write:
            await confirm_tenant_write(
                operations, globals.tenant, yes=globals.yes, interactive=sys.stdin.isatty()
            )
        return await call(operations, cli_scope(globals.tenant))


def _unwrapped(result: object) -> object:
    """The body of a ``Tagged``, ``Saved``, ``Accepted`` or ``Outcome``; any other result as is."""
    if isinstance(result, Tagged | Saved | Accepted | Outcome):
        return result.body
    return result


class _HeldRecords(logging.Filter):
    """Keep the records a handler would print, and print none of them."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def filter(self, record: logging.LogRecord) -> bool:
        self.records.append(record)
        return False


type _Held = list[tuple[logging.Handler, _HeldRecords]]


class _LogHold:
    """Records held back from the CLI's stream handlers until they are released or dropped.

    Args:
        handlers: Handlers to hold records back from; none holds nothing.
    """

    def __init__(self, handlers: list[logging.Handler]) -> None:
        self._held: _Held = [(handler, _HeldRecords()) for handler in handlers]
        for handler, records in self._held:
            handler.addFilter(records)

    def stop(self) -> _Held:
        """Let records through again; return the ones held so far, with their handler."""
        held, self._held = self._held, []
        for handler, records in held:
            handler.removeFilter(records)
        return held

    def release(self) -> None:
        """Print the records held so far, then let records through as they happen."""
        _replay(self.stop())


_HOLD: ContextVar[_LogHold | None] = ContextVar("harborrag_cli_log_hold", default=None)


@contextmanager
def _held_logs() -> Iterator[None]:
    """Hold back the CLI's log output; print it when the block succeeds, drop it otherwise.

    The error envelope, or the one error line, is the whole report of a failure, as over
    HTTP, where the server's log stays with the server. ``HARBORRAG_LOG_LEVEL`` turns the
    hold off, so ``DEBUG`` shows the failure's cause and traceback as they happen.
    ``release_held_logs`` ends the hold early for the rest of the block.
    """
    hold = _LogHold([] if LEVEL_ENV_VAR in os.environ else _cli_log_handlers())
    token = _HOLD.set(hold)
    try:
        yield
    finally:
        _HOLD.reset(token)
        held = hold.stop()
    _replay(held)


def _replay(held: _Held) -> None:
    """Print held records through the handler that held each one."""
    for handler, records in held:
        for record in records.records:
            handler.handle(record)


def _cli_log_handlers() -> list[logging.Handler]:
    """The stream handler the CLI's logging configuration installed, if any."""
    return [
        handler
        for handler in logging.getLogger(ROOT_LOGGER_NAME).handlers
        if isinstance(handler, _ManagedStreamHandler)
    ]


def _control_db_error(health: AppResponse) -> str:
    """Dig the control-plane error out of the health envelope, if it carries one."""
    diagnostics = health.data.get("diagnostics")
    if isinstance(diagnostics, dict):
        runtime = diagnostics.get("runtime")
        if isinstance(runtime, dict):
            control_db = runtime.get("control_db")
            if isinstance(control_db, dict):
                error = control_db.get("error")
                if isinstance(error, str) and error:
                    return error
    return health.error or "runtime not ready"
