"""``harborrag me``: who the CLI acts as."""

from __future__ import annotations

import os
import sys

from harborrag_app.cli.globals import (
    ConfigOption,
    OutputOption,
    ProfileOption,
    TenantOption,
    YesOption,
    resolve_globals,
)
from harborrag_app.cli.invoke import run_operation
from harborrag_app.operations.local import LocalOperations
from harborrag_app.operations.scope import Scope


def command(
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Show the principal, role and tenants the CLI acts with."""
    resolved = resolve_globals(
        tenant=tenant,
        output=output,
        profile=profile,
        config=config,
        yes=yes,
        environ=os.environ,
        stdout_is_tty=sys.stdout.isatty(),
    )
    run_operation("me", _me, globals=resolved)


async def _me(operations: LocalOperations, scope: Scope) -> object:
    return await operations.me(scope.principal, tenant_id=scope.tenant_id)
