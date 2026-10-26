"""Shared fakes for the per-turn conversation-context tests."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda

from harborrag_core.ports.conversation import (
    ConversationIdentity,
    ConversationMessage,
    ConversationRole,
    new_message_id,
)
from harborrag_core.ports.memory import (
    Memory,
    MemoryMatch,
    MemoryOwner,
    MemoryQuery,
    MemoryScope,
    MemoryType,
    ResolvedEntity,
    new_memory_id,
    visible_to,
)

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


@dataclass
class MessageStoreFake:
    """In-memory ``ConversationMessageStore``."""

    values: dict[ConversationIdentity, list[ConversationMessage]] = field(default_factory=dict)

    async def append_messages(
        self, identity: ConversationIdentity, messages: Sequence[ConversationMessage]
    ) -> None:
        self.values.setdefault(identity, []).extend(messages)

    async def recent_messages(
        self, identity: ConversationIdentity, *, limit: int
    ) -> tuple[ConversationMessage, ...]:
        return tuple(self.values.get(identity, [])[-limit:])

    async def messages_after(
        self, identity: ConversationIdentity, *, after_message_id: str | None, limit: int
    ) -> tuple[ConversationMessage, ...]:
        rows = self.values.get(identity, [])
        if after_message_id is None:
            return tuple(rows[:limit])
        ids = [row.message_id for row in rows]
        if after_message_id not in ids:
            raise ValueError("unknown cursor")
        start = ids.index(after_message_id) + 1
        return tuple(rows[start : start + limit])

    async def clear_messages(self, identity: ConversationIdentity) -> None:
        self.values[identity] = []


@dataclass
class MemoryRepositoryFake:
    """In-memory ``MemoryRepository`` that applies the real scope filter."""

    rows: dict[str, Memory] = field(default_factory=dict)
    saved: list[Memory] = field(default_factory=list)
    queries: list[MemoryQuery] = field(default_factory=list)
    fail_search: bool = False
    fail_save: bool = False
    fail_save_after: int | None = None

    async def save(self, memory: Memory) -> None:
        if self.fail_save or (
            self.fail_save_after is not None and len(self.saved) >= self.fail_save_after
        ):
            raise RuntimeError("save unavailable")
        self.rows[memory.memory_id] = memory
        self.saved.append(memory)

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        found = self.rows.get(memory_id)
        if found is None or not visible_to(found.scope, found.owner, caller):
            return None
        return found

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        self.queries.append(query)
        if self.fail_search:
            raise RuntimeError("search unavailable")
        matches = [
            row
            for row in self.rows.values()
            if visible_to(row.scope, row.owner, query.owner)
            and (not query.scopes or row.scope in query.scopes)
            and (not query.memory_types or row.memory_type in query.memory_types)
        ]
        matches.sort(key=lambda row: row.updated_at, reverse=True)
        return tuple(matches[: query.limit])

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        self.rows.pop(memory_id, None)


class ChatModelFake(BaseChatModel):
    """Scripted ``BaseChatModel`` that records the prompts it received."""

    replies: list[str] = []
    structured: list[Any] = []
    failure: str | None = None
    structured_failure: str | None = None
    supports_structured_output: bool = True
    calls: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "harborrag-fake"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        if self.failure is not None:
            raise RuntimeError(self.failure)
        text = self.replies.pop(0) if self.replies else ""
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop, None, **kwargs)

    def with_structured_output(
        self,
        schema: dict[str, Any] | type,
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, Any]:
        """Return a runnable serving the next scripted structured payload."""

        def respond(prompt: LanguageModelInput) -> Any:
            self.calls.append(list(prompt) if isinstance(prompt, list) else [])
            reason = self.structured_failure or self.failure
            if reason is not None:
                raise RuntimeError(reason)
            return self.structured.pop(0) if self.structured else {}

        return RunnableLambda(respond)

    @property
    def prompts(self) -> list[str]:
        """Return the human prompt text of every recorded call."""

        return [call[-1].text for call in self.calls]

    @property
    def systems(self) -> list[str]:
        """Return the system prompt text of every recorded call."""

        return [call[0].text for call in self.calls]


def chat_model(*replies: str, failure: str | None = None) -> ChatModelFake:
    """Build a plain-text fake that advertises no structured-output support."""

    return ChatModelFake(
        replies=list(replies),
        failure=failure,
        calls=[],
        structured=[],
        supports_structured_output=False,
    )


def structured_model(
    *payloads: Any,
    failure: str | None = None,
    replies: Sequence[str] = (),
    structured_failure: str | None = None,
) -> ChatModelFake:
    """Script the structured lane, plus a ``replies`` lane to fall back to."""

    return ChatModelFake(
        replies=list(replies),
        structured=list(payloads),
        failure=failure,
        structured_failure=structured_failure,
        calls=[],
    )


@dataclass
class MemoryIndexFake:
    """In-memory ``MemoryIndex`` scored by fiat over a repository's rows.

    Only rows given an explicit score are considered indexed, so a test says
    exactly what the semantic lane knows. Scope filtering mirrors the real
    contract: the index applies ``visible_to`` itself.
    """

    repository: MemoryRepositoryFake
    scores: dict[str, float] = field(default_factory=dict)
    indexed: list[Memory] = field(default_factory=list)
    embeddings: list[Sequence[float] | None] = field(default_factory=list)
    queries: list[MemoryQuery] = field(default_factory=list)
    fail_search: bool = False

    async def index_memory(
        self, memory: Memory, *, embedding: Sequence[float] | None = None
    ) -> None:
        self.indexed.append(memory)
        self.embeddings.append(embedding)
        self.scores.setdefault(memory.memory_id, 1.0)

    async def search_memories(
        self, query: MemoryQuery, *, embedding: Sequence[float] | None = None
    ) -> tuple[MemoryMatch, ...]:
        self.queries.append(query)
        if self.fail_search:
            raise RuntimeError("index unavailable")
        matches = [
            MemoryMatch(memory_id=row.memory_id, score=self.scores[row.memory_id])
            for row in self.repository.rows.values()
            if row.memory_id in self.scores
            and visible_to(row.scope, row.owner, query.owner)
            and (not query.scopes or row.scope in query.scopes)
            and (not query.memory_types or row.memory_type in query.memory_types)
        ]
        matches.sort(key=lambda match: -match.score)
        return tuple(matches[: query.limit])

    async def delete_memory(self, owner: MemoryOwner, memory_id: str) -> None:
        self.scores.pop(memory_id, None)


@dataclass
class EntityResolverFake:
    """``MemoryEntityResolver`` resolving only the mentions it was told about.

    ``known`` maps a mention to the ``(entity_id, confidence)`` it resolves
    to; a mention absent from it resolves to nothing, exactly as a graph that
    does not know the surface form would behave.
    """

    known: dict[str, tuple[str, float]] = field(default_factory=dict)
    calls: list[tuple[tuple[str, ...], str]] = field(default_factory=list)
    failure: str | None = None

    async def resolve_entities(
        self, mentions: Sequence[str], *, tenant_id: str
    ) -> tuple[ResolvedEntity, ...]:
        self.calls.append((tuple(mentions), tenant_id))
        if self.failure is not None:
            raise RuntimeError(self.failure)
        return tuple(
            ResolvedEntity(mention=mention, entity_id=entity_id, confidence=confidence)
            for mention in mentions
            if (found := self.known.get(mention)) is not None
            for entity_id, confidence in (found,)
        )


@dataclass
class EmbedderFake:
    """Recording ``MemoryEmbedder`` that returns one fixed vector."""

    vector: tuple[float, ...] = (0.5, 0.25)
    texts: list[str] = field(default_factory=list)
    failure: str | None = None

    async def __call__(self, text: str) -> Sequence[float]:
        self.texts.append(text)
        if self.failure is not None:
            raise RuntimeError(self.failure)
        return self.vector


def memory_row(  # noqa: PLR0913 - a test factory for one wide value object
    content: str,
    *,
    scope: MemoryScope = MemoryScope.USER,
    owner: MemoryOwner | None = None,
    memory_type: MemoryType = MemoryType.FACT,
    importance: float = 0.5,
    updated_at: datetime = NOW,
    memory_id: str | None = None,
    **extra: Any,
) -> Memory:
    """Build one memory row with explicit scope, importance, and freshness."""

    return Memory(
        memory_id=memory_id or new_memory_id(),
        scope=scope,
        memory_type=memory_type,
        owner=owner or MemoryOwner(tenant_id="tenant-1", user_id="user-1"),
        content=content,
        importance=importance,
        created_at=updated_at,
        updated_at=updated_at,
        **extra,
    )


def row(
    role: ConversationRole,
    content: str,
    *,
    token_count: int | None = None,
    message_id: str | None = None,
    created_at: datetime = NOW,
    **extra: Any,
) -> ConversationMessage:
    """Build one conversation row with a stable, explicit token cost."""

    return ConversationMessage(
        message_id=message_id or new_message_id(),
        role=role,
        content=content,
        created_at=created_at,
        token_count=token_count,
        **extra,
    )
