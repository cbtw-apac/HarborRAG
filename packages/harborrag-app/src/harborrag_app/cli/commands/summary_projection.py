"""Track, backfill and operate asynchronous graph descriptions."""

import asyncio
import json
from collections.abc import Coroutine
from typing import Annotated, Any

import typer

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.topology import summary_operations

app = typer.Typer(no_args_is_help=True, help="Published-content summary projections.")
Tenant = Annotated[str, typer.Option("--tenant")]


def emit(operation: Coroutine[Any, Any, object]) -> None:
    try:
        result = asyncio.run(operation)
        typer.echo(json.dumps({"ok": True, "data": result}, default=str))
    except Exception as error:
        typer.echo(json.dumps({"ok": False, "error_code": type(error).__name__}), err=True)
        raise typer.Exit(1) from None


@app.command("status")
def status(tenant: Tenant = "DEFAULT") -> None:
    """Show summary job state and safe failure codes; no model calls."""
    emit(summary_operations.status(RuntimeSettings(), tenant))


@app.command("backfill")
def backfill(
    tenant: Tenant = "DEFAULT",
    source_scope: Annotated[str | None, typer.Option("--source-scope")] = None,
) -> None:
    """Synchronize policy and enqueue rebuilds, reusing unchanged generation."""
    emit(summary_operations.backfill(RuntimeSettings(), tenant, source_scope))


@app.command("run-once")
def run_once(tenant: Tenant = "DEFAULT") -> None:
    """Process one ready source without requiring Temporal, graph, or vector services."""
    emit(summary_operations.run_once(RuntimeSettings(), tenant))


@app.command("worker")
def worker(tenant: Tenant = "DEFAULT") -> None:
    """Serve the dedicated summary Temporal queue and reconcile pending work."""
    emit(summary_operations.worker(RuntimeSettings(), tenant))


@app.command("cleanup")
def cleanup(
    tenant: Tenant = "DEFAULT",
    retention_days: Annotated[int, typer.Option(min=1)] = 30,
    apply: Annotated[bool, typer.Option("--apply")] = False,
) -> None:
    """Preview expired replaceable summaries; --apply removes the selected records."""
    emit(
        summary_operations.cleanup(
            RuntimeSettings(), tenant, retention_days=retention_days, apply=apply
        )
    )
