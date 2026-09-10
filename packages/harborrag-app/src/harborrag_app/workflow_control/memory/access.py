"""The authenticated caller's own memory isolation key.

``MemoryIdentity`` covers a *turn* (a session always exists). Administration
is broader: listing and user erasure have no session, so this is the same
identity with ``session_id`` optional. Both are built from the authenticated
principal, never from request fields, which is what makes "the caller's own
memories" enforceable at the storage layer rather than in a route.
"""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.ports.memory import MemoryOwner


@dataclass(frozen=True, slots=True)
class MemoryAccess:
    """Who is asking, and which slice of their memory they are asking about."""

    tenant_id: str
    principal_id: str
    user_id: str
    project_id: str | None = None
    session_id: str | None = None

    def owner(self) -> MemoryOwner:
        """The long-term memory isolation key for this caller."""

        return MemoryOwner(
            tenant_id=self.tenant_id,
            project_id=self.project_id,
            principal_id=self.principal_id,
            user_id=self.user_id,
            session_id=self.session_id,
        )

    def target(self, user_id: str) -> MemoryOwner:
        """The isolation key of another end user in the caller's tenant.

        Used only by the administrative erasure path, where the actor and the
        subject of the erasure are different people.
        """

        return MemoryOwner(
            tenant_id=self.tenant_id,
            principal_id=self.principal_id,
            user_id=user_id,
        )


__all__ = ["MemoryAccess"]
