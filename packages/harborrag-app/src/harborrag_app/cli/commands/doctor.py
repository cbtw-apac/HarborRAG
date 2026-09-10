"""Layered readiness diagnostics."""

from __future__ import annotations

import asyncio
import json
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
    report = asyncio.run(run_doctor(temporal=temporal, service_factory=runner.runtime_app_service))
    data = report.as_payload()
    response = AppResponse(
        report.ok,
        data,
        None if report.ok else "one or more required checks failed",
    )
    if as_json:
        payload = {"ok": response.ok, "data": response.data, "error": response.error}
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    else:
        # The check table itself conveys failure; the renderer's error panel is for
        # operations that could not run at all.
        CliRenderer(no_color=state.no_color).render(AppResponse(True, data), command="doctor")
    if not report.ok:
        raise typer.Exit(1)
