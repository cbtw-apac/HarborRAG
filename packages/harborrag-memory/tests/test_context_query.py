from __future__ import annotations

import pytest
from context_test_fakes import (
    NOW,
    ChatModelFake,
    MessageStoreFake,
    chat_model,
    row,
    structured_model,
)

from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.ports.memory import MemoryType
from harborrag_memory import MemoryContextBuilder, MemoryOwner, MemoryPolicy
from harborrag_memory.context import CondenseResult
from harborrag_memory.context.condenser import MAX_WANTED_TYPES, condense_question
from harborrag_memory.context.prompts import (
    CONDENSE_SYSTEM_PROMPT,
    CONDENSE_TYPED_SYSTEM_PROMPT,
)
from harborrag_memory.errors import MemoryScopeError

pytestmark = [pytest.mark.unit]

QUESTION = "and who owns it?"
HISTORY = (
    ("user", "Who maintains the ingest pipeline?"),
    ("assistant", "Dana Lee maintains the ingest pipeline."),
)


def builder(store: MessageStoreFake, model: ChatModelFake | None) -> MemoryContextBuilder:
    return MemoryContextBuilder(
        policy=MemoryPolicy(recent_max_tokens=500),
        messages=store,
        model=model,
        clock=lambda: NOW,
    )


async def seed(store: MessageStoreFake, identity: ConversationIdentity) -> None:
    await store.append_messages(
        identity,
        [row(role, content, token_count=10) for role, content in HISTORY],  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_follow_up_is_rewritten_from_history(
    store: MessageStoreFake, owner: MemoryOwner, identity: ConversationIdentity
) -> None:
    await seed(store, identity)
    model = chat_model("Who owns the ingest pipeline?")

    context = await builder(store, model).build(owner, QUESTION)

    assert context.standalone_query == "Who owns the ingest pipeline?"
    assert context.rewritten is True
    prompt = model.prompts[0]
    assert "Dana Lee maintains the ingest pipeline." in prompt
    assert QUESTION in prompt


@pytest.mark.asyncio
async def test_first_turn_is_never_rewritten(store: MessageStoreFake, owner: MemoryOwner) -> None:
    model = chat_model("Who owns the ingest pipeline?")

    context = await builder(store, model).build(owner, QUESTION)

    assert context.messages == ()
    assert context.standalone_query == QUESTION
    assert context.rewritten is False
    assert model.calls == []


@pytest.mark.asyncio
async def test_rewrite_is_skipped_when_the_policy_disables_it(
    store: MessageStoreFake, owner: MemoryOwner, identity: ConversationIdentity
) -> None:
    await seed(store, identity)
    model = chat_model("Who owns the ingest pipeline?")
    context = await MemoryContextBuilder(
        policy=MemoryPolicy(query_rewrite=False), messages=store, model=model
    ).build(owner, QUESTION)

    assert context.standalone_query == QUESTION
    assert context.rewritten is False
    assert model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    ["", "   ", "x" * 300, QUESTION],
    ids=["empty", "blank", "oversized", "identical"],
)
async def test_unusable_rewrites_fall_back_to_the_question(
    store: MessageStoreFake, owner: MemoryOwner, identity: ConversationIdentity, reply: str
) -> None:
    await seed(store, identity)

    context = await builder(store, chat_model(reply)).build(owner, QUESTION)

    assert context.standalone_query == QUESTION
    assert context.rewritten is False


@pytest.mark.asyncio
async def test_a_rewrite_failure_degrades_without_raising(
    store: MessageStoreFake, owner: MemoryOwner, identity: ConversationIdentity
) -> None:
    await seed(store, identity)

    context = await builder(store, chat_model(failure="model down")).build(owner, QUESTION)

    assert context.standalone_query == QUESTION
    assert context.rewritten is False
    assert len(context.messages) == 2


@pytest.mark.asyncio
async def test_a_quoted_rewrite_is_unwrapped(
    store: MessageStoreFake, owner: MemoryOwner, identity: ConversationIdentity
) -> None:
    await seed(store, identity)

    context = await builder(store, chat_model('"Who owns the ingest pipeline?"')).build(
        owner, QUESTION
    )

    assert context.standalone_query == "Who owns the ingest pipeline?"
    assert context.rewritten is True


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["session_id", "principal_id"])
async def test_an_owner_without_a_session_is_rejected(
    store: MessageStoreFake, owner: MemoryOwner, missing: str
) -> None:
    from dataclasses import replace

    incomplete = replace(owner, **{missing: None})

    with pytest.raises(MemoryScopeError, match="principal_id and session_id"):
        await builder(store, None).build(incomplete, QUESTION)


REWRITE = "Who owns the ingest pipeline?"


async def condense(model: ChatModelFake) -> CondenseResult | None:
    return await condense_question(
        model,
        question=QUESTION,
        summary=None,
        messages=[row("user", "Who maintains the ingest pipeline?")],
    )


@pytest.mark.asyncio
async def test_the_structured_path_returns_the_rewrite_and_the_wanted_types() -> None:
    model = structured_model({"query": REWRITE, "wanted_types": ["decision", "preference"]})

    result = await condense(model)

    assert result == CondenseResult(
        query=REWRITE, wanted_types=(MemoryType.DECISION, MemoryType.PREFERENCE)
    )
    assert model.systems == [CONDENSE_TYPED_SYSTEM_PROMPT]


def test_the_typed_condense_prompt_defines_the_types_and_distrusts_the_history() -> None:
    for name in ("fact", "preference", "decision", "episode"):
        assert name in CONDENSE_TYPED_SYSTEM_PROMPT
    assert str(MAX_WANTED_TYPES) in CONDENSE_TYPED_SYSTEM_PROMPT
    assert "untrusted" in CONDENSE_TYPED_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_unknown_blank_and_unrecallable_proposed_types_are_dropped() -> None:
    proposed = ["Decision", "", "   ", "nonsense", "summary", "working", "conversation"]

    result = await condense(structured_model({"query": REWRITE, "wanted_types": proposed}))

    assert result == CondenseResult(query=REWRITE, wanted_types=(MemoryType.DECISION,))


@pytest.mark.asyncio
async def test_a_missing_wanted_types_list_is_no_hint_at_all() -> None:
    assert await condense(structured_model({"query": REWRITE})) == CondenseResult(query=REWRITE)


@pytest.mark.asyncio
async def test_the_wanted_types_are_truncated_at_the_cap() -> None:
    proposed = ["episode", "decision", "preference", "fact", "decision"]

    result = await condense(structured_model({"query": REWRITE, "wanted_types": proposed}))

    assert result is not None
    assert len(result.wanted_types) == MAX_WANTED_TYPES
    assert result.wanted_types == (MemoryType.EPISODE, MemoryType.DECISION, MemoryType.PREFERENCE)


@pytest.mark.asyncio
async def test_a_model_without_structured_output_takes_the_plain_text_path() -> None:
    model = chat_model(REWRITE)

    result = await condense(model)

    assert result == CondenseResult(query=REWRITE, wanted_types=())
    assert model.systems == [CONDENSE_SYSTEM_PROMPT]


@pytest.mark.asyncio
async def test_a_failing_structured_call_falls_back_to_the_plain_text_path() -> None:
    model = structured_model(
        {"query": "ignored", "wanted_types": ["decision"]},
        replies=[REWRITE],
        structured_failure="structured output unsupported",
    )

    result = await condense(model)

    assert result == CondenseResult(query=REWRITE, wanted_types=())
    assert model.systems == [CONDENSE_TYPED_SYSTEM_PROMPT, CONDENSE_SYSTEM_PROMPT]


@pytest.mark.asyncio
async def test_an_oversized_structured_rewrite_is_rejected_like_a_plain_one() -> None:
    model = structured_model({"query": "x" * 300, "wanted_types": ["decision"]}, replies=[REWRITE])

    assert await condense(model) == CondenseResult(query=REWRITE, wanted_types=())
    assert await condense(structured_model({"query": "   "})) is None


@pytest.mark.asyncio
async def test_a_question_returned_verbatim_is_no_rewrite_but_still_hints() -> None:
    model = structured_model({"query": QUESTION, "wanted_types": ["decision"]})

    result = await condense(model)

    assert result == CondenseResult(query=QUESTION, wanted_types=(MemoryType.DECISION,))
