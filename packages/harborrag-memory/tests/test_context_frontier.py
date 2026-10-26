from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from context_test_fakes import NOW, MemoryRepositoryFake, MessageStoreFake, chat_model, row

from harborrag_core.ports.conversation import ConversationIdentity, ConversationMessage
from harborrag_memory import MemoryContextBuilder, MemoryOwner, MemoryPolicy
from harborrag_memory.context.builder import MAX_SUMMARY_CATCHUP_PAGES


def messages(start: int, stop: int) -> tuple[ConversationMessage, ...]:
    return tuple(
        row("user" if i % 2 == 0 else "assistant", f"turn-{i}", token_count=20)
        for i in range(start, stop)
    )


@pytest.mark.asyncio
async def test_repeated_history_does_not_resummarize_and_credential_rotation_keeps_owner(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    initial = messages(0, 6)
    await store.append_messages(identity, initial)
    model = chat_model("first", "second")
    builder = MemoryContextBuilder(
        policy=MemoryPolicy(
            recent_max_messages=10,
            recent_max_tokens=100,
            summary_keep_messages=2,
            query_rewrite=False,
            recall_top_k=0,
        ),
        messages=store,
        memories=memories,
        model=model,
        clock=lambda: NOW,
    )
    await builder.build(owner, "q")
    second = await builder.build(owner, "q")
    assert not second.summary_written
    assert second.messages == initial[-2:]
    assert len(model.calls) == len(memories.saved) == 1
    await store.append_messages(identity, messages(6, 8))
    changed = replace(owner, principal_id="new-credential", project_id="new-project")
    third = await builder.build(changed, "q")
    assert third.summary_written
    assert memories.saved[-1].owner == owner
    assert memories.saved[-1].source_message_ids == tuple(m.message_id for m in initial[-2:])


@pytest.mark.asyncio
async def test_frontier_catches_up_history_outside_the_recent_window(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    initial = messages(0, 6)
    await store.append_messages(identity, initial)
    model = chat_model(*[f"summary-{i}" for i in range(10)])
    builder = MemoryContextBuilder(
        policy=MemoryPolicy(
            recent_max_messages=6,
            recent_max_tokens=100,
            summary_keep_messages=2,
            query_rewrite=False,
            recall_top_k=0,
        ),
        messages=store,
        memories=memories,
        model=model,
        clock=lambda: NOW,
    )
    await builder.build(owner, "q")
    added = messages(6, 16)
    await store.append_messages(identity, added)
    result = await builder.build(owner, "q")
    covered = [message_id for memory in memories.saved for message_id in memory.source_message_ids]
    assert covered == [m.message_id for m in (*initial, *added)[:-2]]
    assert result.messages == added[-2:]
    assert all(len(memory.source_message_ids) <= 6 for memory in memories.saved)


@pytest.mark.asyncio
async def test_first_summary_covers_history_older_than_the_recent_window(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    history = messages(0, 16)
    await store.append_messages(identity, history)
    builder = MemoryContextBuilder(
        policy=MemoryPolicy(
            recent_max_messages=6,
            recent_max_tokens=100,
            summary_keep_messages=2,
            query_rewrite=False,
            recall_top_k=0,
        ),
        messages=store,
        memories=memories,
        model=chat_model("one", "two", "three"),
        clock=lambda: NOW,
    )
    result = await builder.build(owner, "q")
    covered = [message_id for memory in memories.saved for message_id in memory.source_message_ids]
    assert covered == [message.message_id for message in history[:-2]]
    assert result.messages == history[-2:]


@pytest.mark.asyncio
async def test_catchup_budget_preserves_latest_window_and_resumes_without_duplicate_work(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    history = messages(0, 6 * (MAX_SUMMARY_CATCHUP_PAGES + 2))
    await store.append_messages(identity, history)
    model = chat_model(*[f"summary-{i}" for i in range(10)])
    builder = MemoryContextBuilder(
        policy=MemoryPolicy(
            recent_max_messages=6,
            recent_max_tokens=100,
            summary_keep_messages=2,
            query_rewrite=False,
            recall_top_k=0,
        ),
        messages=store,
        memories=memories,
        model=model,
        clock=lambda: NOW,
    )
    first = await builder.build(owner, "q")
    assert len(model.calls) == len(memories.saved) == MAX_SUMMARY_CATCHUP_PAGES
    assert first.summary_written
    assert first.messages[-1] == history[-1]
    assert first.summary == memories.saved[-1].content
    first_frontier = memories.saved[-1].source_message_ids[-1]

    second = await builder.build(owner, "q")
    assert second.summary_written
    assert second.messages == history[-2:]
    covered = [message_id for memory in memories.saved for message_id in memory.source_message_ids]
    assert covered == [message.message_id for message in history[:-2]]
    assert covered.count(first_frontier) == 1


@pytest.mark.asyncio
async def test_nonadvancing_history_store_stops_without_repeating_model_calls(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = messages(0, 18)
    await store.append_messages(identity, history)
    page = history[:6]
    reader = AsyncMock(return_value=page)
    monkeypatch.setattr(store, "messages_after", reader)
    model = chat_model("summary", "must not be called")
    builder = MemoryContextBuilder(
        policy=MemoryPolicy(
            recent_max_messages=6,
            recent_max_tokens=100,
            summary_keep_messages=2,
            query_rewrite=False,
            recall_top_k=0,
        ),
        messages=store,
        memories=memories,
        model=model,
        clock=lambda: NOW,
    )
    result = await builder.build(owner, "q")
    assert len(model.calls) == len(memories.saved) == 1
    assert reader.await_count == 2
    assert result.summary == "summary"
    assert result.messages[-1] == history[-1]
    assert memories.saved[0].source_message_ids[-1] == page[-1].message_id
