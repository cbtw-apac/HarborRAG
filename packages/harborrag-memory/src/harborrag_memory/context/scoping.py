"""Narrow a caller's owner to exactly the fields one scope keys on.

Recall and extraction both address one ``MemoryScope`` at a time. Widening an
owner (inventing a ``project_id``, or reusing a session's key for a user-wide
lookup) is a cross-tenant read waiting to happen, so the only transformation
allowed here is dropping fields the scope does not key on -- plus the
``user_id``-from-``principal_id`` derivation the rolling summary already
depends on.
"""

from __future__ import annotations

from harborrag_core.domain.identity import DEFAULT_USER
from harborrag_core.ports.memory import MemoryOwner, MemoryScope, scope_owner_fields

from .summarizer import recall_owner


def scope_query_owner(owner: MemoryOwner, scope: MemoryScope) -> MemoryOwner | None:
    """Return the owner to query ``scope`` as, or ``None`` when it cannot be.

    ``None`` means the caller is missing a field the scope keys on -- an owner
    with no ``project_id`` has no project memories, and inventing one would
    read another project's. ``principal_id`` is dropped because no scope keys
    on it; ``tenant_id`` is always carried since every owner requires one.
    """

    resolved = recall_owner(owner)
    fields = scope_owner_fields(scope)
    values = {name: getattr(resolved, name) for name in fields}
    if any(value is None for value in values.values()):
        return None
    return MemoryOwner(
        tenant_id=resolved.tenant_id,
        project_id=values.get("project_id"),
        user_id=values.get("user_id"),
        session_id=values.get("session_id"),
        run_id=values.get("run_id"),
    )


def personal_scope(owner: MemoryOwner) -> MemoryScope:
    """The scope a PROJECT fact narrows to when the project opt-in is off.

    A real user keeps it as their own USER fact. ``DEFAULT_USER`` is the
    user-less sentinel shared by every such caller in the tenant, across
    projects, so USER scope there would leak the fact; it stays in the session.
    """

    if recall_owner(owner).user_id == DEFAULT_USER:
        return MemoryScope.SESSION
    return MemoryScope.USER


__all__ = ["personal_scope", "scope_query_owner"]
