"""``harborrag doctor``: diagnose the stack with the checks ``/readyz`` runs."""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

import typer

from harborrag_app.cli import invoke
from harborrag_app.cli.doctor import run_doctor
from harborrag_app.cli.exit_codes import exit_code_for
from harborrag_app.cli.globals import (
    ConfigOption,
    OutputOption,
    ProfileOption,
    TenantOption,
    YesOption,
    current_globals,
)
from harborrag_app.cli.output import OutputFormat, print_body, print_error
from harborrag_app.cli.views import render_doctor
from harborrag_app.operations.errors import error_body

logger = logging.getLogger("harborrag.app.cli.doctor")


def command(
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Check project, configuration, services and the tenants that hold data.

    Runs without the API. Exits 0 when every check passes, 6 when a service check fails,
    1 when a configuration check fails.
    """
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    try:
        report = asyncio.run(run_doctor(service_factory=invoke.runtime_app_service))
    except Exception as exc:  # noqa: BLE001 - the CLI's error boundary, as invoke's
        trace_id = uuid4().hex
        logger.debug("doctor failed trace_id=%s", trace_id, exc_info=exc)
        error = error_body(exc)
        print_error(error, output=resolved.output, trace_id=trace_id)
        raise typer.Exit(exit_code_for(error.status)) from None
    payload = report.as_payload()
    if resolved.output is OutputFormat.TABLE:
        render_doctor(payload)
    else:
        print_body(None, payload, output=resolved.output)
    raise typer.Exit(report.exit_code)
