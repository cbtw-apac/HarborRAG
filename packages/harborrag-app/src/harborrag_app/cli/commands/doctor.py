"""Health diagnostics CLI command."""

from __future__ import annotations

from typing import Annotated

import typer

from harborrag_app.cli.runner import invoke
from harborrag_app.workflow_control import AppResponse, BaseAppService


def command(
    context: typer.Context,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit the stable machine-readable response envelope.",
        ),
    ] = False,
) -> None:
    """Check the Temporal workflow service and display connection details."""

    invoke(
        _health,
        context=context,
        command="doctor",
        as_json=as_json,
    )


async def _health(service: BaseAppService) -> AppResponse:
    return await service.runtime_health()
