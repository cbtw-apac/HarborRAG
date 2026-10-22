"""The rolling-summary prompt stays bounded however large one message is (R1g-07)."""

from __future__ import annotations

from typing import Any

import pytest
from context_test_fakes import NOW, ChatModelFake, MemoryRepositoryFake, MessageStoreFake, row
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.ports.memory import MemoryOwner, MemoryType
from harborrag_memory import MemoryContextBuilder, MemoryPolicy
from harborrag_memory.context.builder import SUMMARY_INPUT_MAX_TOKENS, summary_prefix
from harborrag_memory.context.prompting import (
    TRANSCRIPT_MESSAGE_MAX_CHARS,
    clip_message,
    render_transcript,
)
from harborrag_memory.context.summarizer import LAST_COVERED_KEY
from harborrag_memory.context.tokens import approximate_tokens

pytestmark = [pytest.mark.unit]

CONTEXT_LIMIT_CHARS = 32_000
"""About 8k tokens: a small memory model's context window."""


class ContextLimitedModel(ChatModelFake):
    """Plain-text fake that rejects any prompt above ``CONTEXT_LIMIT_CHARS``."""

    def _generate(  # type: ignore[override]
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self.calls.append(list(messages))
        size = sum(len(str(message.content)) for message in messages)
        if size > CONTEXT_LIMIT_CHARS:
            raise RuntimeError(f"context_length_exceeded: {size} chars")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="SUMMARY"))])


@pytest.mark.asyncio
async def test_one_huge_message_no_longer_stalls_the_summary(
    owner: MemoryOwner, identity: ConversationIdentity
) -> None:
    store = MessageStoreFake()
    memories = MemoryRepositoryFake()
    model = ContextLimitedModel(
        replies=[], structured=[], calls=[], supports_structured_output=False
    )
    builder = MemoryContextBuilder(
        policy=MemoryPolicy(query_rewrite=False, recall_top_k=0),
        messages=store,
        memories=memories,
        model=model,
        clock=lambda: NOW,
    )
    huge = row("user", "log line 0123456789 " * 3000)  # 60k chars, a legal chat prompt
    await store.append_messages(identity, [huge, row("assistant", "ok")])

    for turn in range(20):
        await store.append_messages(
            identity, [row("user", f"q{turn}"), row("assistant", f"a{turn}")]
        )
        await builder.build(owner, f"next {turn}")

    prompt_sizes = [sum(len(str(message.content)) for message in call) for call in model.calls]
    assert prompt_sizes
    assert max(prompt_sizes) <= CONTEXT_LIMIT_CHARS
    (summary,) = [m for m in memories.rows.values() if m.memory_type is MemoryType.SUMMARY]
    assert summary.metadata[LAST_COVERED_KEY] != huge.message_id
    assert "characters omitted" in model.prompts[0]


def test_clip_message_keeps_head_and_tail_with_an_elision_marker() -> None:
    text = "HEAD" + "x" * 20_000 + "TAIL"

    clipped = clip_message(text)

    assert clipped.startswith("HEAD")
    assert clipped.endswith("TAIL")
    assert "characters omitted" in clipped
    assert len(clipped) < TRANSCRIPT_MESSAGE_MAX_CHARS + 100
    assert clip_message("  short  ") == "short"


def test_render_transcript_clips_each_message() -> None:
    rendered = render_transcript([row("user", "y" * 50_000), row("assistant", "fine")])

    assert len(rendered) < TRANSCRIPT_MESSAGE_MAX_CHARS + 200
    assert rendered.endswith("Assistant: fine")


def test_summary_prefix_caps_tokens_but_always_takes_one_message() -> None:
    big = [row("user", "z" * 7_000) for _ in range(10)]

    taken = summary_prefix(big, approximate_tokens)

    assert 1 <= taken < len(big)
    assert sum(approximate_tokens(clip_message(m.content)) for m in big[:taken]) <= (
        SUMMARY_INPUT_MAX_TOKENS
    )
    assert summary_prefix(big[:1], lambda _text: SUMMARY_INPUT_MAX_TOKENS * 10) == 1


@pytest.mark.asyncio
async def test_a_token_capped_catch_up_page_resumes_where_its_summary_stopped(
    owner: MemoryOwner, identity: ConversationIdentity
) -> None:
    store = MessageStoreFake()
    memories = MemoryRepositoryFake()
    history = [
        row("user" if i % 2 == 0 else "assistant", f"m{i} " + "w" * 7_000) for i in range(10)
    ]
    await store.append_messages(identity, history)
    model = ContextLimitedModel(
        replies=[], structured=[], calls=[], supports_structured_output=False
    )
    builder = MemoryContextBuilder(
        policy=MemoryPolicy(
            recent_max_messages=4, summary_keep_messages=2, query_rewrite=False, recall_top_k=0
        ),
        messages=store,
        memories=memories,
        model=model,
        clock=lambda: NOW,
    )

    await builder.build(owner, "where are we?")

    covered = [message_id for saved in memories.saved for message_id in saved.source_message_ids]
    assert len(memories.saved) >= 2
    assert covered == [message.message_id for message in history[: len(covered)]]
