"""Caller identity threaded from the transport into conversation and long-term memory."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.ports.memory import MemoryOwner


@dataclass(frozen=True, slots=True)
class MemoryIdentity:
    """Who a chat/agent turn is remembered for.

    ``principal_id`` is the authenticated subject that owns the session;
    ``user_id`` is the stable end-user identity (``HARBORRAG_AUTH_USER_ID_CLAIM``)
    that user-scoped memory is keyed by; ``project_id`` is the validated
    project the turn happened in, when the caller supplied one.
    """

    tenant_id: str
    principal_id: str
    user_id: str
    session_id: str
    project_id: str | None = None

    @classmethod
    def build(
        cls,
        *,
        tenant_id: str,
        principal_id: str,
        session_id: str,
        user_id: str | None = None,
        project_id: str | None = None,
    ) -> MemoryIdentity:
        """Assemble the identity, defaulting ``user_id`` to the principal."""

        return cls(
            tenant_id=tenant_id,
            principal_id=principal_id,
            user_id=user_id or principal_id,
            session_id=session_id,
            project_id=project_id,
        )

    def owner(self) -> MemoryOwner:
        """The long-term memory isolation key for this caller."""

        return MemoryOwner(
            tenant_id=self.tenant_id,
            project_id=self.project_id,
            principal_id=self.principal_id,
            user_id=self.user_id,
            session_id=self.session_id,
        )

    def conversation(self) -> ConversationIdentity:
        """The conversation-history isolation key for this session.

        ``user_id`` is what owns the conversation: one service principal
        fronting several people must not make ``session_id`` the only thing
        keeping their histories apart.
        """

        return ConversationIdentity(
            self.tenant_id,
            self.principal_id,
            self.session_id,
            self.user_id,
        )


__all__ = ["MemoryIdentity"]
