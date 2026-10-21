"""Long-running CLI flows that pair a service call with inline live progress."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import typer

from harborrag_app.cli import runner
from harborrag_app.cli.progress import ProgressUnavailable, StatusSource, TaskSource
from harborrag_app.cli.progress_view import LiveProgress
from harborrag_app.cli.rendering import CliRenderer
from harborrag_app.cli.rendering_values import mapping
from harborrag_app.workflow_control import AppResponse, BaseAppService

type ResponseCall = Callable[[BaseAppService], Awaitable[AppResponse]]

_DIRECT_INTERVAL = 0.5
INTERRUPTED_EXIT_CODE = 130


def invoke_run(  # noqa: PLR0913 - one parameter per CLI presentation option
    call: ResponseCall,
    *,
    context: typer.Context,
    run_id: str,
    label: str,
    as_json: bool,
    events: bool,
    interval: float = _DIRECT_INTERVAL,
) -> None:
    """Execute a direct ingestion while following its task-store progress."""

    code = _run_to_exit_code(
        _run(
            call,
            run_id=run_id,
            label=label,
            state=runner.state(context),
            as_json=as_json,
            events=events,
            interval=interval,
        )
    )
    if code:
        raise typer.Exit(code)


async def _run(  # noqa: PLR0913 - mirrors invoke_run
    call: ResponseCall,
    *,
    run_id: str,
    label: str,
    state: runner.CliState,
    as_json: bool,
    events: bool,
    interval: float,
) -> int:
    renderer = CliRenderer(no_color=state.no_color)
    machine = as_json or events
    service = await runner.build_service(renderer, command="ingest", action="run", as_json=machine)
    if service is None:
        return 1
    try:
        unavailable = runner.control_plane_failure(service)
        if unavailable is not None:
            runner.emit(
                unavailable, renderer=renderer, command="ingest", action="run", as_json=machine
            )
            return 1
        task = asyncio.ensure_future(call(service))
        try:
            if events or not as_json:
                live = LiveProgress(
                    renderer.console,
                    TaskSource(service, run_id),
                    label=label,
                    interval=interval,
                    events=events,
                )
                try:
                    await live.follow(until=task)
                except ProgressUnavailable:
                    pass  # the final envelope below still reports the outcome
            response = await task
        except asyncio.CancelledError:
            # Ctrl-C reaches an `asyncio.run` program as a cancelled main task. The
            # ingestion runs in this process, so it must stop rather than keep writing
            # underneath a closing service.
            await _settle(task)
            renderer.error_console.print(
                f"Cancelled inline run {run_id}.", markup=False, highlight=False
            )
            raise
        except Exception as exc:  # noqa: BLE001 - CLI owns the stable error boundary
            await _settle(task)
            response = runner.failure(exc)
        runner.emit(response, renderer=renderer, command="ingest", action="run", as_json=machine)
        return 0 if response.ok and run_completed(response) else 1
    finally:
        await runner.close(service)


def invoke_start_and_follow(
    call: ResponseCall,
    *,
    context: typer.Context,
    interval: float,
    timeout: float | None = None,
) -> None:
    """Submit a durable run, follow its status inline, then print the final result."""

    code = _run_to_exit_code(
        _start_and_follow(call, state=runner.state(context), interval=interval, timeout=timeout)
    )
    if code:
        raise typer.Exit(code)


async def _start_and_follow(
    call: ResponseCall,
    *,
    state: runner.CliState,
    interval: float,
    timeout: float | None,
) -> int:
    renderer = CliRenderer(no_color=state.no_color)
    service = await runner.build_service(renderer, command="ingest", action="start", as_json=False)
    if service is None:
        return 1
    try:
        unavailable = runner.control_plane_failure(service)
        if unavailable is not None:
            runner.emit(
                unavailable, renderer=renderer, command="ingest", action="start", as_json=False
            )
            return 1
        try:
            started = await call(service)
        except Exception as exc:  # noqa: BLE001 - CLI owns the stable error boundary
            started = runner.failure(exc)
        runner.emit(started, renderer=renderer, command="ingest", action="start", as_json=False)
        if not started.ok:
            return 1
        run_id = str(mapping(started.data.get("run")).get("run_id", ""))
        return await _follow_status(
            service, renderer, run_id=run_id, interval=interval, events=False, timeout=timeout
        )
    finally:
        await runner.close(service)


def invoke_watch(
    run_id: str,
    *,
    context: typer.Context,
    interval: float,
    events: bool,
    timeout: float | None = None,
) -> None:
    """Follow an existing durable run inline until it settles (Ctrl-C leaves it running)."""

    code = _run_to_exit_code(
        _watch(
            run_id, state=runner.state(context), interval=interval, events=events, timeout=timeout
        )
    )
    if code:
        raise typer.Exit(code)


async def _watch(
    run_id: str,
    *,
    state: runner.CliState,
    interval: float,
    events: bool,
    timeout: float | None,
) -> int:
    renderer = CliRenderer(no_color=state.no_color)
    service = await runner.build_service(renderer, command="ingest", action="watch", as_json=events)
    if service is None:
        return 1
    try:
        # `watch` reads run state out of the control plane, so the same gate that guards
        # `ingest status` and `ingest wait` applies here.
        unavailable = runner.control_plane_failure(service)
        if unavailable is not None:
            runner.emit(
                unavailable, renderer=renderer, command="ingest", action="watch", as_json=events
            )
            return 1
        return await _follow_status(
            service, renderer, run_id=run_id, interval=interval, events=events, timeout=timeout
        )
    finally:
        await runner.close(service)


async def _settle(task: asyncio.Future[AppResponse]) -> None:
    """Cancel and drain a scheduled call so it never outlives the service it uses."""

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def run_completed(response: AppResponse) -> bool:
    """Only a fully completed run is a success.

    ``IngestionTaskState`` also carries PARTIAL and CANCELLED; both mean the requested
    work did not finish, so both must leave a non-zero exit code behind.
    """

    status = str(mapping(response.data.get("result")).get("status", "")).lower()
    return status == "completed"


async def _follow_status(  # noqa: PLR0913 - one parameter per CLI presentation option
    service: BaseAppService,
    renderer: CliRenderer,
    *,
    run_id: str,
    interval: float,
    events: bool,
    timeout: float | None = None,
) -> int:
    live = LiveProgress(
        renderer.console,
        StatusSource(service, run_id),
        label=run_id,
        interval=interval,
        events=events,
    )
    try:
        async with asyncio.timeout(timeout) as deadline:
            await live.follow()
            result = await service.ingestion_result(run_id)
    except ProgressUnavailable as exc:
        response = AppResponse(False, {"error_type": "ProgressUnavailable"}, str(exc))
        runner.emit(response, renderer=renderer, command="ingest", action="watch", as_json=events)
        return 1
    except TimeoutError:
        if not deadline.expired():
            raise
        status = live.last.status if live.last is not None else None
        await runner.report_timeout(service, renderer, run_id, status)
        return runner.TIMEOUT_EXIT_CODE
    except asyncio.CancelledError:
        renderer.error_console.print(
            f"Left run {run_id} running; re-attach with: harborrag ingest watch {run_id}",
            markup=False,
            soft_wrap=True,
            highlight=False,
        )
        raise
    runner.emit(result, renderer=renderer, command="ingest", action="wait", as_json=events)
    # Same rule as `ingest run`: a durable run that ended PARTIAL or CANCELLED did not
    # finish the requested work either.
    return 0 if result.ok and run_completed(result) else 1


def _run_to_exit_code(coroutine: Coroutine[Any, Any, int]) -> int:
    """Run a follow coroutine; an un-signalled cancellation still means "interrupted"."""

    try:
        return asyncio.run(coroutine)
    except asyncio.CancelledError:
        return INTERRUPTED_EXIT_CODE


__all__ = ["invoke_run", "invoke_start_and_follow", "invoke_watch", "run_completed"]
