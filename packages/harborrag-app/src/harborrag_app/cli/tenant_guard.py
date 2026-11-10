"""Ask before the first write into a tenant that holds no data.

A tenant ID names the tenant's storage, and an owner's write creates it on first use, so a
typo in ``--tenant`` would create a new tenant. The question is asked only on the client;
the operations and the HTTP contract do not change.

Start from: confirm_tenant_write.
"""

from __future__ import annotations

import click

from harborrag_app.operations.local import LocalOperations


async def confirm_tenant_write(
    operations: LocalOperations, tenant_id: str, *, yes: bool, interactive: bool
) -> None:
    """Return when the write may go ahead; ask first when the tenant holds no data.

    Args:
        operations: Operations that can tell whether the tenant holds data.
        tenant_id: Tenant the write goes to.
        yes: ``--yes`` was given, so nothing is checked or asked.
        interactive: stdin is a terminal, so the question can be answered.

    Raises:
        click.UsageError: The tenant holds no data, there is no terminal and no ``--yes``.
        click.Abort: The user declined; Click exits 1.
    """
    if yes or await operations.tenant_has_data(tenant_id):
        return
    if not interactive:
        raise click.UsageError(f"Tenant `{tenant_id}` has no data yet; pass --yes to create it.")
    click.confirm(
        f"Tenant `{tenant_id}` has no data yet; this creates it. Continue?",
        abort=True,
        err=True,
    )
