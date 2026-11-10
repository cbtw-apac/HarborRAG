"""Who the CLI acts as in local mode: ``cli:<os-user>``, an owner of every tenant.

Local mode runs the operations in this process, so the OS user stands in for a verified
credential. That subject is what writes record in ``created_by``, ``updated_by`` and
``triggered_by``.

Start from: cli_scope.
"""

from __future__ import annotations

import getpass

from harborrag_app.api.auth.principal import Principal
from harborrag_app.operations.scope import Scope


def cli_principal() -> Principal:
    """Return ``cli:<os-user>`` with the ``owner`` role over every tenant (``*``)."""
    return Principal(
        subject=f"cli:{getpass.getuser()}",
        role="owner",
        tenant_ids=frozenset({"*"}),
        token_kind="cli",
    )


def cli_scope(tenant_id: str) -> Scope:
    """Return the local-mode principal acting in ``tenant_id``."""
    return Scope(cli_principal(), tenant_id)
