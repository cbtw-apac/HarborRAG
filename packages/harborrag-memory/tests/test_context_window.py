from __future__ import annotations

from dataclasses import replace

import pytest
from context_test_fakes import (
    NOW,
    ChatModelFake,
    MemoryRepositoryFake,
    MessageStoreFake,
    chat_model,
    row,
)

from harborrag_core.ports.conversation import ConversationIdentity, ConversationMessage
from harborrag_memory import MemoryContextBuilder, MemoryOwner, MemoryPolicy
from harborrag_memory.context.summarizer import LAST_COVERED_KEY, SummaryRecord

pytestmark = [pytest.mark.unit]


def conversation(count: int, *, tokens: int) -> list[ConversationMessage]:
    """Build ``count`` alternating user/assistant rows of a fixed token cost."""

    return [
        row("user" if index % 2 == 0 else "assistant", f"turn-{index}", token_count=tokens)
        for index in range(count)
    ]


def builder(
    *,
    policy: MemoryPolicy,
    store: MessageStoreFake,
    memories: MemoryRepositoryFake | None = None,
    model: ChatModelFake | None = None,
) -> MemoryContextBuilder:
    return MemoryContextBuilder(
        policy=policy,
        messages=store,
        memories=memories,
        model=model,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_disabled_policy_returns_a_plain_window_without_calling_the_model(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    rows = conversation(8, tokens=500)
    await store.append_messages(identity, rows)
    model = chat_model("never used")

    context = await builder(
        policy=MemoryPolicy.disabled(), store=store, memories=memories, model=model
    ).build(owner, "what now?")

    assert context.messages == tuple(rows[-4:])
    assert context.summary is None
    assert context.recalled == ()
    assert context.standalone_query == "what now?"
    assert context.rewritten is False
    assert context.summary_written is False
    assert model.calls == []
    assert memories.saved == []


@pytest.mark.asyncio
async def test_window_is_token_trimmed_from_the_oldest_end(
    store: MessageStoreFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    rows = conversation(6, tokens=30)
    await store.append_messages(identity, rows)
    policy = MemoryPolicy(recent_max_tokens=100, query_rewrite=False)

    context = await builder(policy=policy, store=store).build(owner, "what now?")

    assert context.messages == (rows[4], rows[5])
    assert context.messages[0].role == "user"
    assert sum(message.token_count or 0 for message in context.messages) <= 100
    assert context.summary is None


@pytest.mark.asyncio
async def test_a_budget_smaller_than_one_message_keeps_the_newest_message(
    store: MessageStoreFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    rows = conversation(3, tokens=50)
    await store.append_messages(identity, rows)
    policy = MemoryPolicy(recent_max_tokens=10, query_rewrite=False)

    context = await builder(policy=policy, store=store).build(owner, "what now?")

    assert context.messages == (rows[-1],)


@pytest.mark.asyncio
async def test_summary_triggers_past_the_threshold_and_shrinks_the_window(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    rows = conversation(6, tokens=20)
    await store.append_messages(identity, rows)
    policy = MemoryPolicy(
        recent_max_messages=10,
        recent_max_tokens=100,
        summary_keep_messages=2,
        query_rewrite=False,
    )
    model = chat_model("Dana owns ingestion; the retention question is open.")

    context = await builder(policy=policy, store=store, memories=memories, model=model).build(
        owner, "what now?"
    )

    assert context.summary == "Dana owns ingestion; the retention question is open."
    assert context.summary_written is True
    assert context.messages == (rows[4], rows[5])

    assert len(memories.saved) == 1
    assert list(memories.rows) == ["summary:s-1"]
    saved = memories.saved[0]
    assert saved.memory_id == "summary:s-1"
    assert saved.scope.value == "session"
    assert saved.memory_type.value == "summary"
    assert saved.source_session_id == "s-1"
    assert saved.source_message_ids == tuple(message.message_id for message in rows[:4])
    assert saved.metadata[LAST_COVERED_KEY] == rows[3].message_id
    assert saved.updated_at == NOW == saved.valid_from
    assert "turn-0" in model.prompts[0]
    assert "turn-5" not in model.prompts[0]


@pytest.mark.asyncio
async def test_second_trigger_folds_the_prior_summary_into_a_replacement(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    rows = conversation(6, tokens=20)
    await store.append_messages(identity, rows)
    policy = MemoryPolicy(
        recent_max_messages=10,
        recent_max_tokens=100,
        summary_keep_messages=2,
        query_rewrite=False,
    )
    model = chat_model("first summary text", "second summary text")
    context_builder = builder(policy=policy, store=store, memories=memories, model=model)

    await context_builder.build(owner, "first question")
    second = await context_builder.build(owner, "second question")

    assert second.summary == "second summary text"
    assert second.summary_written is True
    assert "first summary text" in model.prompts[1]
    assert len(memories.saved) == 2
    assert list(memories.rows) == ["summary:s-1"]
    assert memories.rows["summary:s-1"].content == "second summary text"
    assert memories.rows["summary:s-1"].source_message_ids == tuple(
        message.message_id for message in rows[:4]
    )


@pytest.mark.asyncio
async def test_stored_summary_is_reused_when_nothing_triggers(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    rows = conversation(2, tokens=10)
    await store.append_messages(identity, rows)
    await memories.save(
        SummaryRecord(
            owner=owner,
            summary="Dana owns ingestion.",
            covered=(rows[0],),
            now=NOW,
        ).to_memory()
    )
    policy = MemoryPolicy(recent_max_tokens=100, summary_keep_messages=1, query_rewrite=False)
    model = chat_model("never used")

    context = await builder(policy=policy, store=store, memories=memories, model=model).build(
        owner, "what now?"
    )

    assert context.summary == "Dana owns ingestion."
    assert context.summary_written is False
    assert context.messages == tuple(rows)
    assert model.calls == []
    assert len(memories.saved) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["model", "repository"])
async def test_summary_failures_degrade_to_the_prior_summary(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
    failing: str,
) -> None:
    rows = conversation(6, tokens=20)
    await store.append_messages(identity, rows)
    await memories.save(
        SummaryRecord(owner=owner, summary="prior summary", covered=(rows[0],), now=NOW).to_memory()
    )
    memories.saved.clear()
    memories.fail_save = failing == "repository"
    model = chat_model("fresh summary", failure="model down" if failing == "model" else None)
    policy = MemoryPolicy(
        recent_max_messages=10,
        recent_max_tokens=150,
        summary_keep_messages=2,
        query_rewrite=False,
    )

    context = await builder(policy=policy, store=store, memories=memories, model=model).build(
        owner, "what now?"
    )

    assert context.summary == "prior summary"
    assert context.summary_written is False
    assert context.messages == tuple(rows)
    assert memories.saved == []


@pytest.mark.asyncio
async def test_a_search_failure_loses_the_summary_without_failing_the_turn(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    rows = conversation(2, tokens=10)
    await store.append_messages(identity, rows)
    memories.fail_search = True
    policy = MemoryPolicy(recent_max_tokens=100, query_rewrite=False)

    context = await builder(policy=policy, store=store, memories=memories).build(owner, "q")

    assert context.summary is None
    assert context.messages == tuple(rows)


def test_trim_window_falls_back_to_a_tail_when_langchain_trimming_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harborrag_memory.context import trimming

    rows = conversation(5, tokens=30)

    def boom(*args: object, **kwargs: object) -> list[object]:
        raise RuntimeError("trim unavailable")

    monkeypatch.setattr(trimming, "trim_messages", boom)

    kept = trimming.trim_window(rows, max_tokens=100, counter=len)

    assert kept == (rows[2], rows[3], rows[4])
    assert kept[0].role == "user"


@pytest.mark.asyncio
async def test_a_summary_round_trips_for_an_owner_without_a_user_id(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    identity: ConversationIdentity,
) -> None:
    """SESSION scope keys on ``user_id``; it is derived from ``principal_id``."""

    owner = MemoryOwner(
        tenant_id=identity.tenant_id,
        principal_id=identity.principal_id,
        session_id=identity.session_id,
    )
    rows = conversation(6, tokens=20)
    # Conversation history is user-scoped too, so seed it under the identity
    # this owner derives: user_id falls back to principal_id.
    await store.append_messages(replace(identity, user_id=identity.principal_id), rows)
    policy = MemoryPolicy(
        recent_max_messages=10,
        recent_max_tokens=100,
        summary_keep_messages=2,
        query_rewrite=False,
    )
    model = chat_model("first summary", "second summary")
    context_builder = builder(policy=policy, store=store, memories=memories, model=model)

    first = await context_builder.build(owner, "q1")
    second = await context_builder.build(owner, "q2")

    assert first.summary == "first summary"
    assert "first summary" in model.prompts[1]
    assert second.summary == "second summary"
    assert await memories.get(memories.saved[-1].owner, "summary:s-1") is not None
