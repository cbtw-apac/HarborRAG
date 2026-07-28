"""Tenant-scoped hybrid retrieval command."""

from __future__ import annotations

from typing import Annotated

import typer

from harborrag_app.cli.runner import invoke


# Typer requires each public CLI option to remain a separate function parameter.
def command(  # noqa: PLR0913
    context: typer.Context,
    query: Annotated[
        str,
        typer.Argument(
            metavar="QUERY",
            help="Semantic query text; it is not echoed in command output.",
        ),
    ],
    tenant_id: Annotated[
        str,
        typer.Option(
            "--tenant",
            metavar="TENANT_ID",
            help="Tenant whose active indexes may be searched.",
        ),
    ],
    top_k: Annotated[
        int,
        typer.Option(
            "--top-k",
            min=1,
            max=100,
            metavar="COUNT",
            help="Maximum number of fused results.",
        ),
    ] = 10,
    include_content: Annotated[
        bool,
        typer.Option(
            "--include-content",
            help="Include retrieved document text; disabled by default.",
        ),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit the stable machine-readable response envelope.",
        ),
    ] = False,
) -> None:
    """Search active Qdrant vectors and expand matching FalkorDB context."""

    invoke(
        lambda service: service.retrieve(
            query,
            tenant_id=tenant_id,
            top_k=top_k,
            include_content=include_content,
        ),
        context=context,
        command="retrieve",
        as_json=as_json,
    )
