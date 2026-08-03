"""One-shot chat completion command."""

from __future__ import annotations

from typing import Annotated

import typer

from harborrag_app.cli.runner import invoke
from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest
from harborrag_runtime.chat import ChatPrompt


# Typer requires each public CLI option to remain a separate function parameter.
def command(  # noqa: PLR0913
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
        str | None,
        typer.Option(
            "--system",
            metavar="TEXT",
            help="Optional request-specific system message after the selected prompt.",
        ),
    ] = None,
    prompt: Annotated[
        ChatPrompt,
        typer.Option(
            "--prompt",
            help="Server-owned prompt template to prepend.",
        ),
    ] = ChatPrompt.DEFAULT,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            metavar="LOGICAL_MODEL",
            help="Configured logical model; defaults to the model catalog selection.",
        ),
    ] = None,
    temperature: Annotated[
        float,
        typer.Option("--temperature", min=0.0, max=2.0, help="Sampling temperature."),
    ] = 0.2,
    max_tokens: Annotated[
        int,
        typer.Option(
            "--max-tokens",
            min=1,
            max=32_768,
            help="Maximum generated tokens.",
        ),
    ] = 1024,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the stable machine-readable response envelope."),
    ] = False,
) -> None:
    """Generate one response with the configured Harbor chat client."""

    messages = []
    if system is not None:
        messages.append(HarborChatMessage.system(system))
    messages.append(HarborChatMessage.user(message))
    request = HarborChatRequest(
        messages=tuple(messages),
        logical_model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        sensitive=True,
    )
    invoke(
        lambda service: service.chat_completion(
            request,
            tenant_id=tenant_id,
            principal_id="harborrag-cli",
            prompt=prompt,
        ),
        context=context,
        command="chat",
        as_json=as_json,
    )
