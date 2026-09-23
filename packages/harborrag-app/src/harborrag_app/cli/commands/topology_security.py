"""Operator-supplied resolved permissions never infer grants from connector metadata."""

from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    no_args_is_help=True, help="Tenant mode, spending limits and trusted resolved ACL imports."
)
File = Annotated[Path, typer.Argument(exists=True, dir_okay=False)]


def read_bounded(path: Path) -> str:
    with path.open(encoding="utf-8") as stream:
        data = stream.read(1_000_001)
    if len(data) > 1_000_000:
        raise typer.BadParameter("operator configuration exceeds 1 MB")
    return data


@app.command("configure")
def configure(path: File) -> None:
    """Apply TenantIndexingConfig JSON; default enabled=false, prohibited overrides source policy."""
    from harborrag_core.topology.config import parse_indexing_configuration
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.security_operations import configure_indexing

    from .topology import _emit

    try:
        config = parse_indexing_configuration(read_bounded(path))
    except ValueError:
        raise typer.BadParameter("invalid tenant indexing configuration JSON") from None
    _emit(configure_indexing(RuntimeSettings(), config))


@app.command("status")
def status(tenant: Annotated[str, typer.Option("--tenant")] = "DEFAULT") -> None:
    """Read effective tenant mode, epoch, spending pause and budget limits."""
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.security_operations import indexing_status

    from .topology import _emit

    _emit(indexing_status(RuntimeSettings(), tenant))


@app.command("permissions-import")
def permissions_import(path: File) -> None:
    """Import trusted effective principal decisions, not unresolved groups or inherited ACLs.

    Unknown/expired snapshots deny access. Supply both source and document scopes.
    Group expansion, inherited denies and ABAC must be resolved by the source adapter.
    """
    from harborrag_core.topology.permissions import ResolvedPermissionSnapshot
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.security_operations import import_permissions

    from .topology import _emit

    try:
        snapshot = ResolvedPermissionSnapshot.model_validate_json(read_bounded(path))
    except ValueError:
        raise typer.BadParameter("invalid resolved permission snapshot JSON") from None
    _emit(import_permissions(RuntimeSettings(), snapshot))


@app.command("permissions-status")
def permissions_status(
    tenant: Annotated[str, typer.Option("--tenant")] = "DEFAULT",
) -> None:
    """Report current ACL coverage counts for active sources and documents."""

    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.topology.security_operations import permission_coverage

    from .topology import _emit

    _emit(permission_coverage(RuntimeSettings(), tenant))
