"""Ask before a write the operator may not mean: into a tenant with no data, or a deletion.

A tenant ID names the tenant's storage, and an owner's write creates it on first use, so a
typo in ``--tenant`` would create a new tenant. The question is asked only on the client;
the operations and the HTTP contract do not change.

Start from: confirm_tenant_write, confirm_deletion.
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


def confirm_deletion(question: str, *, yes: bool, interactive: bool) -> None:
    """Return when the deletion may go ahead; ask first unless ``--yes`` was given.

    Args:
        question: What the deletion removes, as a question.
        yes: ``--yes`` was given, so nothing is asked.
        interactive: stdin is a terminal, so the question can be answered.

    Raises:
        click.UsageError: There is no terminal and no ``--yes``.
        click.Abort: The user declined; Click exits 1.
    """
    if yes:
        return
    if not interactive:
        raise click.UsageError("Pass --yes to delete without a terminal.")
    click.confirm(question, abort=True, err=True)
