"""Service lifecycle and response presentation shared by CLI commands."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import typer

from harborrag_app.cli.rendering import CliRenderer
from harborrag_app.workflow_control import AppResponse, BaseAppService
from harborrag_app.workflow_control.selection import runtime_app_service

type ResponseCall = Callable[[BaseAppService], Awaitable[AppResponse]]


@dataclass(frozen=True, slots=True)
class CliState:
    """Presentation options propagated through the Typer context tree."""

    no_color: bool = False


def invoke(
    call: ResponseCall,
    *,
    context: typer.Context,
    command: str,
    action: str | None = None,
    as_json: bool = False,
) -> None:
    """Execute one application-service call and render its stable response."""

    exit_code = asyncio.run(
        _invoke(
            call,
            state=_state(context),
            command=command,
            action=action,
            as_json=as_json,
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


async def _invoke(
    call: ResponseCall,
    *,
    state: CliState,
    command: str,
    action: str | None,
    as_json: bool,
) -> int:
    renderer = CliRenderer(no_color=state.no_color)
    try:
        service = runtime_app_service()
    except Exception as exc:  # noqa: BLE001 - CLI owns the stable error boundary
        _emit(
            _failure(exc),
            renderer=renderer,
            command=command,
            action=action,
            as_json=as_json,
        )
        return 1
    try:
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

    service = runtime_app_service()
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
        error=str(exc) or type(exc).__name__,
    )


def _state(context: typer.Context) -> CliState:
    value = context.find_root().obj
    return value if isinstance(value, CliState) else CliState()


def _operation_message(command: str, action: str | None) -> str:
    if command == "doctor":
        return "Checking Temporal runtime…"
    messages = {
        "start": "Starting ingestion workflow…",
        "status": "Loading ingestion status…",
        "wait": "Waiting for ingestion to finish…",
        "pause": "Pausing ingestion…",
        "resume": "Resuming ingestion…",
        "cancel": "Cancelling ingestion…",
        "retry": "Submitting artifact retries…",
    }
    return (
        messages.get(action, "Running HarborRAG command…")
        if action
        else "Running HarborRAG command…"
    )
