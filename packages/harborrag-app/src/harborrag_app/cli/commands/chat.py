"""``harborrag chat``: one grounded answer with numbered citations."""

from __future__ import annotations

from typing import Annotated

import typer

from harborrag_app.api.v1.chat.schemas import ChatCompletionRequest, ChatCompletionResponse
from harborrag_app.cli.bodies import load_body
from harborrag_app.cli.globals import (
    ConfigOption,
    OutputOption,
    ProfileOption,
    TenantOption,
    YesOption,
    current_globals,
)
from harborrag_app.cli.invoke import current_trace_id, run_operation
from harborrag_app.operations.local import LocalOperations
from harborrag_app.operations.scope import Scope
from harborrag_core.invariants import HarborInvariantError


def command(  # noqa: PLR0913 - one parameter per documented flag plus the global options
    message: Annotated[
        str, typer.Argument(metavar="MESSAGE", help="Question for the configured chat model.")
    ],
    session_id: Annotated[
        str | None, typer.Option("--session-id", metavar="SESSION_ID", help="Continue a session.")
    ] = None,
    mode: Annotated[
        str | None, typer.Option("--mode", help="instant or classic (default: classic).")
    ] = None,
    project_id: Annotated[
        list[str] | None,
        typer.Option(
            "--project-id", metavar="PROJECT_ID", help="One-off questions: read only this project."
        ),
    ] = None,
    connection_ref: Annotated[
        list[str] | None,
        typer.Option(
            "--connection-ref",
            metavar="REF",
            help="Narrow to this connection, PROJECT_ID/CONNECTION_ID; repeat for more.",
        ),
    ] = None,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Retrieve supporting evidence and answer one question with numbered citations."""
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    request = load_body(
        None,
        ChatCompletionRequest,
        flags={
            "session_id": session_id,
            "message": message,
            "mode": mode,
            "project_ids": project_id or None,
            "connections": connection_ref or None,
            "stream": False,
        },
    )

    async def call(operations: LocalOperations, scope: Scope) -> ChatCompletionResponse:
        completion = await operations.create_chat_completion(
            scope, request, idempotency_key=None, trace_id=current_trace_id()
        )
        if completion.body is None:
            raise HarborInvariantError("a non-streaming completion carried no body")
        return completion.body

    run_operation("create_chat_completion", call, globals=resolved)
