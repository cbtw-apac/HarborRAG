"""Task-scoped lease ownership shared by conversation repository implementations."""

from __future__ import annotations

from contextvars import ContextVar

from harborrag_core.ports.conversation import ConversationIdentity


class ConversationLeaseContext:
    """Carry acquired fencing tokens into writes and cancellation finalizers.

    Each repository owns its context. Child tasks inherit a snapshot, so a
    delayed write retains its original token and cannot impersonate the next
    lease holder. Copy-on-write avoids sharing mutable state between tasks.
    """

    def __init__(self) -> None:
        self._tokens: ContextVar[dict[ConversationIdentity, str] | None] = ContextVar(
            "conversation_lease_tokens", default=None
        )

    def token(self, identity: ConversationIdentity) -> str | None:
        return (self._tokens.get() or {}).get(identity)

    def bind(self, identity: ConversationIdentity, token: str) -> None:
        self._tokens.set({**(self._tokens.get() or {}), identity: token})

    def release(self, identity: ConversationIdentity, token: str) -> None:
        tokens = dict(self._tokens.get() or {})
        if tokens.get(identity) == token:
            del tokens[identity]
            self._tokens.set(tokens or None)
