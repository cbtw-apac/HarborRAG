"""One derivation of the conversation identity a memory owner maps to."""

from __future__ import annotations

from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.ports.memory import MemoryOwner

from .errors import MemoryScopeError


def conversation_identity(owner: MemoryOwner, *, subject: str) -> ConversationIdentity:
    """Map a memory owner onto the conversation it reads and writes.

    Shared because the short-term tier, the per-turn context builder and the
    summarizer each derived this separately, comment and all: three copies of
    one rule about who owns a conversation, any of which could drift.

    ``subject`` only shapes the error, so each caller keeps naming itself.
    """

    if owner.principal_id is None or owner.session_id is None:
        raise MemoryScopeError(f"{subject} requires principal_id and session_id")
    return ConversationIdentity(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        session_id=owner.session_id,
        # Conversation history is owned by the human; fall back to the
        # credential only for owners predating user-scoped ownership, which
        # is exactly what migration 0024 backfilled.
        user_id=owner.user_id or owner.principal_id,
    )


__all__ = ["conversation_identity"]
