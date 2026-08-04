"""One-shot, retrieval-grounded chat completion command."""

from __future__ import annotations

from typing import Annotated

import typer

from harborrag_app.cli.runner import invoke
from harborrag_runtime.chat import ChatPrompt


def command(
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
    system: Annotated[
        ChatPrompt,
        typer.Option(
            "--system",
            help="Server-owned system prompt to apply.",
        ),
    ] = ChatPrompt.DEFAULT,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the stable machine-readable response envelope."),
    ] = False,
) -> None:
    """Retrieve supporting evidence and generate one grounded response."""

    invoke(
        lambda service: service.chat_completion(
            message,
            tenant_id=tenant_id,
            principal_id="harborrag-cli",
            system=system,
        ),
        context=context,
        command="chat",
        as_json=as_json,
    )
