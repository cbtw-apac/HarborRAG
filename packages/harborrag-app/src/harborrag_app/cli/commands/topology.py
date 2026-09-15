"""Explicit operator controls for optional asynchronous semantic topology."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer

from .topology_security import app as indexing_app

app = typer.Typer(no_args_is_help=True, help="Configure and operate semantic topology enrichment.")
config_app = typer.Typer(no_args_is_help=True, help="Validate and apply graph_build.yaml.")
resolution_app = typer.Typer(
    no_args_is_help=True, help="Audited, reversible entity identity decisions."
)
app.add_typer(resolution_app, name="resolution")
app.add_typer(indexing_app, name="indexing")
app.add_typer(config_app, name="config")
Tenant = Annotated[str, typer.Option("--tenant", help="Tenant authority and evidence scope.")]
Scope = Annotated[str, typer.Option("--source-scope", help="Canonical ingestion source scope ID.")]


@app.command("evaluate")
def evaluate(
    cases: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Evaluate expected/predicted extractions in JSONL offline; no provider requests."""
    from harborrag_runtime.topology.operations import evaluate as evaluate_cases

    with cases.open(encoding="utf-8") as stream:
        payload = stream.read(20_000_001)
    _emit(evaluate_cases(payload))


@app.command("audit")
def audit(
    tenant: Tenant = "DEFAULT",
    after_build_id: Annotated[str | None, typer.Option("--after-build-id")] = None,
) -> None:
    """Check at most 100 eligible semantic graph builds for projection drift."""
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.maintenance import audit as audit_builds

    _emit(audit_builds(RuntimeSettings(), tenant, after_build_id=after_build_id))


@app.command("cleanup")
def cleanup(
    tenant: Tenant = "DEFAULT",
    after_build_id: Annotated[str | None, typer.Option("--after-build-id")] = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Delete retired graph and derived-vector projections.",
        ),
    ] = False,
) -> None:
    """Preview retired builds; --apply deletes projections while retaining frozen data."""
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.maintenance import cleanup as clean_builds

    _emit(clean_builds(RuntimeSettings(), tenant, apply=apply, after_build_id=after_build_id))


@app.command("entities")
def entities(
    principal: Annotated[
        str, typer.Option("--principal", help="Resolved tenant principal; no ACL bypass.")
    ],
    tenant: Tenant = "DEFAULT",
    label: Annotated[str | None, typer.Option("--label", help="Exact entity label.")] = None,
) -> None:
    """List at most 100 currently supported mentions and their opaque entity IDs."""
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.operations import entities as list_entities

    _emit(list_entities(RuntimeSettings(), tenant, label, principal_id=principal))


@app.command("inspect")
def inspect(
    build_id: Annotated[str, typer.Argument(help="Accepted topology build ID.")],
    principal: Annotated[
        str, typer.Option("--principal", help="Resolved tenant principal; no ACL bypass.")
    ],
    tenant: Tenant = "DEFAULT",
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20,
) -> None:
    """Show LLM descriptions, typed facts, exact evidence, and frozen provenance."""
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.operations import inspect_build

    _emit(inspect_build(RuntimeSettings(), tenant, build_id, principal_id=principal, limit=limit))


@resolution_app.command("merge")
def merge(
    entity_ids: Annotated[list[str], typer.Argument(help="Two or more known entity IDs.")],
    decision_id: Annotated[str, typer.Option("--decision-id", help="Unique idempotency key.")],
    actor: Annotated[str, typer.Option("--actor", help="Operator identity for audit.")],
    reason: Annotated[str, typer.Option("--reason", help="Evidence supporting this decision.")],
    tenant: Tenant = "DEFAULT",
) -> None:
    """Merge same-type identities; hide old topology until topology-only backfill finishes."""
    from harborrag_core.topology.resolution import ResolutionRequest
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.operations import resolve

    request = ResolutionRequest(
        tenant_id=tenant,
        decision_id=decision_id,
        action="merge",
        entity_ids=tuple(entity_ids),
        actor=actor,
        reason=reason,
    )
    _emit(resolve(RuntimeSettings(), request))


@resolution_app.command("revert")
def revert(
    prior_decision_id: str,
    decision_id: Annotated[
        str, typer.Option("--decision-id", help="New unique audit decision ID.")
    ],
    actor: Annotated[str, typer.Option("--actor")],
    reason: Annotated[str, typer.Option("--reason")],
    tenant: Tenant = "DEFAULT",
) -> None:
    """Record a reversal and enqueue resolution-only rebuilds from frozen extraction."""
    from harborrag_core.topology.resolution import ResolutionRequest
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.operations import resolve

    request = ResolutionRequest(
        tenant_id=tenant,
        decision_id=decision_id,
        action="revert",
        reverts_decision_id=prior_decision_id,
        actor=actor,
        reason=reason,
    )
    _emit(resolve(RuntimeSettings(), request))


@resolution_app.command("list")
def resolution_list(tenant: Tenant = "DEFAULT") -> None:
    """Inspect the most recent 100 immutable resolution decisions."""
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.operations import resolutions

    _emit(resolutions(RuntimeSettings(), tenant))


def _emit(operation: Coroutine[Any, Any, object]) -> None:
    try:
        result = asyncio.run(operation)
    except Exception as error:
        # Provider/SQL exceptions can contain private source data or credentials.
        typer.echo(json.dumps({"ok": False, "error_code": type(error).__name__}), err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps({"ok": True, "data": result}, default=str))


def _graph_build_settings(path: Path | None) -> Any:
    from harborrag_runtime.config.settings import RuntimeSettings

    settings = RuntimeSettings()
    return settings.model_copy(update={"graph_build_config_path": path}) if path else settings


@config_app.command("validate")
def validate_config(
    path: Annotated[Path | None, typer.Option("--path", dir_okay=False)] = None,
) -> None:
    """Validate graph-build policy and display its non-secret effective values."""
    from harborrag_runtime.config.graph_build import (
        GraphBuildConfig,
        graph_build_runtime_view,
    )

    try:
        settings = _graph_build_settings(path)
        config = GraphBuildConfig.from_settings(settings)
        effective = config.effective_settings(settings)
    except Exception as error:
        typer.echo(json.dumps({"ok": False, "error_code": type(error).__name__}), err=True)
        raise typer.Exit(1) from None
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "data": {
                    "runtime": graph_build_runtime_view(effective),
                    "tenants": config.model_dump(mode="json")["tenants"],
                },
            },
            default=str,
        )
    )


@config_app.command("apply")
def apply_config(
    path: Annotated[Path | None, typer.Option("--path", dir_okay=False)] = None,
) -> None:
    """Synchronize managed tenant/source policy to canonical Postgres state."""
    from harborrag_runtime.topology.operations import apply_graph_build_config

    _emit(apply_graph_build_config(_graph_build_settings(path)))


@app.command("profile")
def profile(
    model: Annotated[
        str | None, typer.Option("--model", help="Single-deployment extraction model.")
    ] = None,
) -> None:
    """Print a pinned profile template from the model catalog (no model request)."""
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.operations import make_profile

    try:
        value = make_profile(RuntimeSettings(), model)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from None
    typer.echo(value.model_dump_json(indent=2))


@app.command("enable")
def enable(
    source_scope: Scope,
    profile_file: Annotated[Path, typer.Option("--profile", exists=True, dir_okay=False)],
    tenant: Tenant = "DEFAULT",
) -> None:
    """Enable a pinned extraction policy and enqueue unchanged active documents."""
    from harborrag_core.topology import ExtractionProfile, TopologyPolicy
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.operations import configure

    try:
        selected = ExtractionProfile.model_validate_json(profile_file.read_text())
    except ValueError:
        raise typer.BadParameter(
            "profile must contain a valid extraction profile JSON object"
        ) from None
    _emit(
        configure(
            RuntimeSettings(),
            TopologyPolicy(
                tenant_id=tenant,
                source_scope_id=source_scope,
                enabled=True,
                profile=selected,
            ),
        )
    )


@app.command("disable")
def disable(source_scope: Scope, tenant: Tenant = "DEFAULT") -> None:
    """Revoke serving eligibility and prevent new work for a source scope."""
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.operations import disable as disable_policy

    _emit(disable_policy(RuntimeSettings(), tenant, source_scope))


@app.command("status")
def status(tenant: Tenant = "DEFAULT") -> None:
    """List recent jobs without returning extracted source text."""
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.operations import status as read_status

    _emit(read_status(RuntimeSettings(), tenant))


@app.command("run")
def run(
    tenant: Tenant = "DEFAULT",
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
) -> None:
    """Drain a bounded number of durable enrichment attempts directly."""
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.operations import run as run_jobs

    _emit(run_jobs(RuntimeSettings(), tenant, limit=limit))


@app.command("worker")
def worker(
    tenant: Tenant = "DEFAULT",
    temporal: Annotated[
        bool, typer.Option("--temporal", help="Run an independent Temporal worker and dispatcher.")
    ] = False,
) -> None:
    """Continuously reconcile and process jobs until interrupted."""
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.operations import run as run_jobs

    settings = RuntimeSettings()
    if temporal:
        from harborrag_runtime.topology.worker import run_temporal_worker

        _emit(run_temporal_worker(settings, tenant))
    else:
        _emit(run_jobs(settings, tenant, watch=True))


@app.command("rebuild")
def rebuild(
    build_id: Annotated[str, typer.Argument()],
    tenant: Tenant = "DEFAULT",
) -> None:
    """Restore an eligible graph build from immutable artifacts without an LLM."""
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.operations import rebuild as rebuild_projection

    _emit(rebuild_projection(RuntimeSettings(), tenant, build_id))


@app.command("derive")
def derive(build_id: str, tenant: Tenant = "DEFAULT") -> None:
    """Build budgeted contextual/parent artifacts independently of accepted topology."""
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.operations import derive as derive_artifacts

    _emit(derive_artifacts(RuntimeSettings(), tenant, build_id))
