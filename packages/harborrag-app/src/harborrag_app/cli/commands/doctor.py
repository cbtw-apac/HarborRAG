"""Layered readiness diagnostics."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from harborrag_app.cli import runner
from harborrag_app.cli.doctor import run_doctor
from harborrag_app.cli.rendering import CliRenderer
from harborrag_app.workflow_control import AppResponse


def command(
    context: typer.Context,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the stable machine-readable response envelope."),
    ] = False,
    temporal: Annotated[
        bool,
        typer.Option("--temporal", help="Also check the Temporal workflow service."),
    ] = False,
) -> None:
    """Check project, configuration, environment, and local services."""

    state = runner.state(context)
    renderer = CliRenderer(no_color=state.no_color)
    try:
        report = asyncio.run(
            run_doctor(temporal=temporal, service_factory=runner.runtime_app_service)
        )
    except Exception as exc:  # noqa: BLE001 - CLI owns the stable error boundary
        # The checks that build a service can raise; without this the CLI would print a
        # raw traceback instead of the envelope automation parses.
        runner.emit(
            runner.failure(exc),
            renderer=renderer,
            command="doctor",
            action=None,
            as_json=as_json,
        )
        raise typer.Exit(1) from None
    data = report.as_payload()
    if as_json:
        # runner.emit owns the machine-readable envelope; a second copy here would drift.
        runner.emit(
            AppResponse(
                report.ok,
                data,
                None if report.ok else "one or more required checks failed",
            ),
            renderer=renderer,
            command="doctor",
            action=None,
            as_json=True,
        )
    else:
        # The check table itself conveys failure; the renderer's error panel is for
        # operations that could not run at all.
        renderer.render(AppResponse(True, data), command="doctor")
    if not report.ok:
        raise typer.Exit(1)
