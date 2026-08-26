"""Tenant-scoped hybrid retrieval command."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from harborrag_app.cli.runner import invoke
from harborrag_runtime.sdk import RetrievalLane


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
    ] = "DEFAULT",
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
    lane: Annotated[
        RetrievalLane,
        typer.Option(
            "--lane",
            help="Retrieval lane: dense, sparse, or hybrid.",
        ),
    ] = RetrievalLane.HYBRID,
    filters_json: Annotated[
        str | None,
        typer.Option(
            "--filters-json",
            metavar="JSON",
            help="Metadata filters as a JSON object; tenant_id is not allowed here.",
        ),
    ] = None,
    observe_graph: Annotated[
        bool,
        typer.Option(
            "--graph/--no-graph",
            help="Observe graph context for the leading retrieval candidates.",
        ),
    ] = True,
    include_content: Annotated[
        bool,
        typer.Option(
            "--include-content",
            help="Include retrieved document text; disabled by default.",
        ),
    ] = False,
    include_metadata: Annotated[
        bool,
        typer.Option(
            "--include-metadata",
            help="Include result metadata; disabled by default.",
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

    filters = _filters(filters_json)
    invoke(
        lambda service: service.retrieve(
            query,
            tenant_id=tenant_id,
            top_k=top_k,
            filters=filters,
            lane=lane,
            observe_graph=observe_graph,
            include_content=include_content,
            include_metadata=include_metadata,
        ),
        context=context,
        command="retrieve",
        as_json=as_json,
    )


def _filters(value: str | None) -> dict[str, object]:
    if value is None:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("filters must be a valid JSON object") from exc
    if not isinstance(parsed, dict):
        raise typer.BadParameter("filters must be a JSON object")
    if "tenant_id" in parsed:
        raise typer.BadParameter("tenant_id must be provided with --tenant")
    return parsed
