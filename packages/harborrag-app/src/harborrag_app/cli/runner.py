"""Response presentation shared by ``doctor``; the other commands run on ``cli.invoke``."""

from __future__ import annotations

import json
from dataclasses import dataclass

import typer

from harborrag_app.cli.rendering import CliRenderer
from harborrag_app.workflow_control import AppResponse
from harborrag_app.workflow_control.composition.selection import (
    runtime_app_service as runtime_app_service,
)
from harborrag_app.workflow_control.errors import public_error_message
from harborrag_core.contracts.errors import HarborConfigurationError


@dataclass(frozen=True, slots=True)
class CliState:
    """Presentation options propagated through the Typer context tree."""

    no_color: bool = False


def emit(
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


def failure(exc: Exception) -> AppResponse:
    # HarborConfigurationError messages are authored in this repository and name
    # HARBORRAG_* variables, never their values; they are the operator's whole
    # diagnostic. Forwarded here, not via _PUBLIC_MESSAGE_TYPES, which also feeds HTTP
    # responses (see doctor.environment.check_error_detail).
    if isinstance(exc, HarborConfigurationError):
        error = str(exc) or type(exc).__name__
    else:
        error = public_error_message(exc)
    return AppResponse(False, data={"error_type": type(exc).__name__}, error=error)


def state(context: typer.Context) -> CliState:
    value = context.find_root().obj
    return value if isinstance(value, CliState) else CliState()
