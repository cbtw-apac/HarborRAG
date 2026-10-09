"""LangChain chat-message history backed by HarborRAG's conversation message store."""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import BaseMessage

from harborrag_core.ports.conversation import ConversationIdentity, ConversationMessageStore
from harborrag_core.ports.memory import MemoryOwner, MemoryRepository

from ..context.summarizer import delete_session_summaries
from .converters import Clock, from_langchain_messages, to_langchain_messages, utc_now

_SYNC_UNSUPPORTED = (
    "HarborChatMessageHistory is async-only; use aget_messages(), aadd_messages(), "
    "or aclear() (LangChain runnables call these when invoked with ainvoke/astream)"
)


class HarborChatMessageHistory(BaseChatMessageHistory):
    """Expose one conversation session's messages through LangChain's history contract.

    The history is scoped by an authenticated ``ConversationIdentity``; build it
    from request context, never from user-supplied fields. Reads return the last
    ``recent_limit`` messages oldest-first, and writes append in call order.
    Pass ``memories`` so ``aclear`` also deletes the session's rolling summary.
    """

    def __init__(  # noqa: PLR0913 - one keyword-only collaborator per optional port
        self,
        store: ConversationMessageStore,
        identity: ConversationIdentity,
        *,
        recent_limit: int = 50,
        run_id: str | None = None,
        clock: Clock = utc_now,
        memories: MemoryRepository | None = None,
    ) -> None:
        if recent_limit < 1:
            raise ValueError("recent_limit must be at least 1")
        self._store = store
        self._identity = identity
        self._recent_limit = recent_limit
        self._run_id = run_id
        self._clock = clock
        self._memories = memories

    @property
    def identity(self) -> ConversationIdentity:
        """Return the session this history reads and writes."""

        return self._identity

    @property
    def messages(self) -> list[BaseMessage]:  # type: ignore[override]
        raise NotImplementedError(_SYNC_UNSUPPORTED)

    async def aget_messages(self) -> list[BaseMessage]:
        """Return the most recent messages of the session, oldest-first."""

        rows = await self._store.recent_messages(self._identity, limit=self._recent_limit)
        return to_langchain_messages(rows)

    def add_messages(self, messages: Sequence[BaseMessage]) -> None:
        raise NotImplementedError(_SYNC_UNSUPPORTED)

    async def aadd_messages(self, messages: Sequence[BaseMessage]) -> None:
        """Append ``messages`` to the session in the given order."""

        if not messages:
            return
        rows = from_langchain_messages(messages, run_id=self._run_id, clock=self._clock)
        await self._store.append_messages(self._identity, rows)

    def clear(self) -> None:
        raise NotImplementedError(_SYNC_UNSUPPORTED)

    async def aclear(self) -> None:
        """Delete every message of the session, and its rolling summaries when wired."""

        await self._store.clear_messages(self._identity)
        if self._memories is not None:
            await delete_session_summaries(
                self._memories,
                MemoryOwner(
                    tenant_id=self._identity.tenant_id,
                    user_id=self._identity.user_id,
                    principal_id=self._identity.principal_id or None,
                    session_id=self._identity.session_id,
                ),
            )


__all__ = ["HarborChatMessageHistory"]
