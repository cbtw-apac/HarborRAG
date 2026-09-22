from __future__ import annotations

import pytest
from context_test_fakes import (
    NOW,
    MemoryRepositoryFake,
    MessageStoreFake,
    chat_model,
    row,
)

from harborrag_core.ports.conversation import ConversationIdentity, ConversationMessage
from harborrag_memory import MemoryContextBuilder, MemoryOwner, MemoryPolicy
from harborrag_memory.context.trimming import keep_boundary, trim_window

pytestmark = [pytest.mark.unit]

TOOL_CALLS = '[{"id": "c1", "name": "search", "args": {"q": "owner"}}]'


def tool_conversation() -> list[ConversationMessage]:
    """A conversation whose newest turn is an assistant tool call plus its result."""

    return [
        row("user", "hello there", token_count=20, message_id="m0"),
        row("assistant", "hi", token_count=20, message_id="m1"),
        row("user", "find the owner", token_count=20, message_id="m2"),
        row("assistant", "", token_count=20, message_id="m3", tool_calls_json=TOOL_CALLS),
        row("tool", "Dana Lee", token_count=20, message_id="m4", tool_call_id="c1"),
        row("assistant", "Dana Lee owns it.", token_count=20, message_id="m5"),
    ]


def test_trimming_never_leaves_a_tool_result_without_its_tool_call() -> None:
    rows = tool_conversation()

    assert [m.message_id for m in trim_window(rows, max_tokens=200, counter=len)] == [
        "m0",
        "m1",
        "m2",
        "m3",
        "m4",
        "m5",
    ]
    for budget in (100, 60, 40, 10):
        kept = trim_window(rows, max_tokens=budget, counter=len)
        assert kept, budget
        assert kept[0].role == "user" or kept == (rows[-1],), budget
        assert not any(message.role == "tool" for message in kept) or "m3" in {
            message.message_id for message in kept
        }, budget


def test_keep_boundary_walks_back_to_the_owning_user_turn() -> None:
    rows = tool_conversation()

    assert keep_boundary(rows, 2) == 2
    assert keep_boundary(rows, 3) == 2
    assert keep_boundary(rows, 4) == 2
    assert keep_boundary(rows, 6) == 0
    assert keep_boundary(rows[3:], 2) == 1
    assert keep_boundary([], 2) == 0


@pytest.mark.asyncio
async def test_the_post_summary_window_starts_on_a_user_turn(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    rows = tool_conversation()
    await store.append_messages(identity, rows)
    context = await MemoryContextBuilder(
        policy=MemoryPolicy(
            recent_max_messages=10,
            recent_max_tokens=100,
            summary_keep_messages=2,
            query_rewrite=False,
        ),
        messages=store,
        memories=memories,
        model=chat_model("Dana owns ingestion."),
        clock=lambda: NOW,
    ).build(owner, "what now?")

    assert context.summary_written is True
    assert [message.message_id for message in context.messages] == ["m2", "m3", "m4", "m5"]
    assert memories.saved[0].source_message_ids == ("m0", "m1")


def test_message_text_prefers_the_property_over_the_deprecated_call(
    recwarn: pytest.WarningsRecorder,
) -> None:
    import warnings

    from langchain_core.messages import AIMessage

    from harborrag_memory.context.prompting import content_text, message_text

    warnings.simplefilter("always")
    assert message_text(AIMessage(content="hello")) == "hello"
    assert [w for w in recwarn.list if "text() as a method" in str(w.message)] == []
    assert content_text([{"type": "text", "text": "a"}, "b"]) == "ab"
    assert content_text(7) == ""


@pytest.mark.asyncio
async def test_a_single_user_turn_window_writes_no_summary(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    """One question plus a long tool chain has nothing older than the boundary."""

    rows = [row("user", "find the owner", token_count=20, message_id="m0")]
    for index in range(11):
        rows.append(
            row("assistant", "", token_count=20, message_id=f"a{index}", tool_calls_json=TOOL_CALLS)
            if index % 2 == 0
            else row("tool", "Dana", token_count=20, message_id=f"t{index}", tool_call_id="c1")
        )
    await store.append_messages(identity, rows)

    context = await MemoryContextBuilder(
        policy=MemoryPolicy(recent_max_tokens=1000, query_rewrite=False),
        messages=store,
        memories=memories,
        model=chat_model("unused summary"),
        clock=lambda: NOW,
    ).build(owner, "what now?")

    assert context.summary_written is False
    assert context.summary is None
    assert memories.saved == []
    assert context.messages == tuple(rows)
