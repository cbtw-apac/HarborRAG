"""One-shot, retrieval-grounded chat completion command."""

from __future__ import annotations

from typing import Annotated

import typer

from harborrag_app.cli.runner import invoke
from harborrag_app.workflow_control import AppResponse, BaseAppService
from harborrag_app.workflow_control.chat import ChatExecutionOptions
from harborrag_runtime.chat import ChatPrompt


def command(  # noqa: PLR0913 - explicit CLI flags are the public command contract
    context: typer.Context,
    message: Annotated[
        str,
        typer.Argument(
            metavar="MESSAGE",
            help="User message sent to the configured chat model.",
        ),
    ],
    tenant_id: Annotated[
        str,
        typer.Option("--tenant", metavar="TENANT_ID", help="Tenant identity for model policy."),
    ] = "DEFAULT",
    session_id: Annotated[
        str | None,
        typer.Option("--session", metavar="SESSION_ID", help="Conversation memory session key."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the stable machine-readable response envelope."),
    ] = False,
) -> None:
    """Retrieve supporting evidence and generate one grounded response."""

    invoke(
        lambda service: _complete(
            service,
            message=message,
            tenant_id=tenant_id,
            session_id=session_id,
        ),
        context=context,
        command="chat",
        as_json=as_json,
    )


async def _complete(
    service: BaseAppService,
    *,
    message: str,
    tenant_id: str,
    session_id: str | None,
) -> AppResponse:
    if session_id is None:
        created = await service.create_chat_session(
            tenant_id=tenant_id,
            principal_id="harborrag-cli",
        )
        if not created.ok:
            return created
        session_id = str(created.data["session_id"])
    return await service.chat_completion(
        message,
        tenant_id=tenant_id,
        principal_id="harborrag-cli",
        options=ChatExecutionOptions(
            session_id=session_id,
            system=ChatPrompt.DEFAULT,
        ),
    )
