"""Service lifecycle and response presentation shared by CLI commands."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import typer

from harborrag_app.cli.rendering import CliRenderer
from harborrag_app.workflow_control import AppResponse, BaseAppService
from harborrag_app.workflow_control.composition.selection import runtime_app_service
from harborrag_app.workflow_control.errors import public_error_message

type ResponseCall = Callable[[BaseAppService], Awaitable[AppResponse]]

logger = logging.getLogger("harborrag.app.cli.runner")


@dataclass(frozen=True, slots=True)
class CliState:
    """Presentation options propagated through the Typer context tree."""

    no_color: bool = False


def invoke(  # noqa: PLR0913 - one parameter per independent CLI presentation option
    call: ResponseCall,
    *,
    context: typer.Context,
    command: str,
    action: str | None = None,
    as_json: bool = False,
    requires_control_plane: bool = True,
) -> None:
    """Execute one application-service call and render its stable response.

    ``requires_control_plane`` gates the command on a usable control-plane database.
    Composition deliberately boots degraded when migrations fail, so a service that
    cannot reach its schema is still returned; a command that then does real work fails
    much later with an opaque error such as a missing column. Diagnostic commands set
    this to ``False`` -- a degraded control plane is exactly when they are run.
    """

    exit_code = asyncio.run(
        _invoke(
            call,
            state=_state(context),
            command=command,
            action=action,
            as_json=as_json,
            requires_control_plane=requires_control_plane,
        )
    )
    if exit_code:
        raise typer.Exit(exit_code)


def invoke_dashboard(
    run_id: str,
    *,
    context: typer.Context,
    refresh_seconds: float,
) -> None:
    """Run the Textual dashboard inside the service's async lifecycle."""

    try:
        asyncio.run(_run_dashboard(run_id, refresh_seconds=refresh_seconds))
    except Exception as exc:  # noqa: BLE001 - CLI owns the stable error boundary
        renderer = CliRenderer(no_color=_state(context).no_color)
        renderer.render(
            _failure(exc),
            command="ingest",
            action="watch",
        )
        raise typer.Exit(1) from None


async def _invoke(  # noqa: PLR0913 - mirrors invoke()'s option surface
    call: ResponseCall,
    *,
    state: CliState,
    command: str,
    action: str | None,
    as_json: bool,
    requires_control_plane: bool = True,
) -> int:
    renderer = CliRenderer(no_color=state.no_color)
    try:
        logger.debug("Building the runtime application service")
        service = await asyncio.to_thread(runtime_app_service)
    except Exception as exc:  # noqa: BLE001 - CLI owns the stable error boundary
        logger.exception("Failed to build the runtime application service")
        _emit(
            _failure(exc),
            renderer=renderer,
            command=command,
            action=action,
            as_json=as_json,
        )
        return 1
    try:
        unavailable = _control_plane_failure(service) if requires_control_plane else None
        if unavailable is not None:
            _emit(
                unavailable,
                renderer=renderer,
                command=command,
                action=action,
                as_json=as_json,
            )
            return 1
        try:
            with renderer.operation(
                _operation_message(command, action),
                enabled=not as_json,
            ):
                response = await call(service)
        except Exception as exc:  # noqa: BLE001 - CLI owns the stable error boundary
            response = _failure(exc)
        _emit(
            response,
            renderer=renderer,
            command=command,
            action=action,
            as_json=as_json,
        )
        return 0 if response.ok else 1
    finally:
        await _close(service)


async def _run_dashboard(run_id: str, *, refresh_seconds: float) -> None:
    from harborrag_app.cli.dashboard import IngestionDashboard

    logger.debug("Building the runtime application service for the dashboard")
    service = await asyncio.to_thread(runtime_app_service)
    try:
        dashboard = IngestionDashboard(
            run_id,
            service,
            refresh_seconds=refresh_seconds,
        )
        await dashboard.run_async()
    finally:
        await _close(service)


def _emit(
    response: AppResponse,
    *,
    renderer: CliRenderer,
    command: str,
    action: str | None,
    as_json: bool,
) -> None:
    if as_json:
        payload = {
            "ok": response.ok,
            "data": response.data,
            "error": response.error,
        }
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return
    renderer.render(response, command=command, action=action)


async def _close(service: BaseAppService) -> None:
    close = getattr(service, "aclose", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def _failure(exc: Exception) -> AppResponse:
    return AppResponse(
        False,
        data={"error_type": type(exc).__name__},
        error=public_error_message(exc),
    )


def _control_plane_failure(service: BaseAppService) -> AppResponse | None:
    """Return a failure response when the control plane is unusable, else ``None``."""

    try:
        health = service.health()
    except Exception:
        # A service that cannot report its own health is not evidence that the control
        # plane is broken. Let the command run and fail on its own terms rather than
        # blocking it on an inconclusive check.
        logger.debug("Control-plane readiness check unavailable", exc_info=True)
        return None
    if health.ok:
        return None
    detail = _control_db_error(health)
    logger.error("Control-plane readiness check failed: %s", detail)
    return AppResponse(
        False,
        data={"error_type": "ControlPlaneUnavailable"},
        error=(
            "Control plane is not ready. Refusing to run "
            "against a database whose schema may be stale; run 'harborrag doctor' for "
            "diagnostics."
        ),
    )


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


def _state(context: typer.Context) -> CliState:
    value = context.find_root().obj
    return value if isinstance(value, CliState) else CliState()


def _operation_message(command: str, action: str | None) -> str:
    if command == "doctor":
        return "Checking Temporal runtime…"
    if command == "chat":
        return "Generating chat response…"
    messages = {
        "start": "Starting ingestion workflow…",
        "status": "Loading ingestion status…",
        "wait": "Waiting for ingestion to finish…",
        "pause": "Pausing ingestion…",
        "resume": "Resuming ingestion…",
        "cancel": "Cancelling ingestion…",
    }
    return (
        messages.get(action, "Running HarborRAG command…")
        if action
        else "Running HarborRAG command…"
    )
