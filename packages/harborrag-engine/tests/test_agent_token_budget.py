"""Tests for the agent loop's aggregate token budget and budget-stop synthesis."""

from __future__ import annotations

import pytest
from agent_test_helpers import Chat, Runs, Spec, Tools
from agent_test_helpers import response as _response

from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatUsage,
    HarborToolCall,
    HarborToolCallFunction,
    MessageRole,
)
from harborrag_core.ports.agent_runs import AgentRunIdentity, AgentStopReason
from harborrag_engine.agent import AgentRunOptions, AgentService
from harborrag_engine.agent.guard import ExecutionGuard
from harborrag_engine.agent.loop_state import LoopState, RunContext
from harborrag_engine.agent.service import _AGENT_INSTRUCTIONS
from harborrag_engine.agent.token_budget import (
    SYNTHESIS_RESERVE_TOKENS,
    TokenBudgetExhausted,
    completion_token_limit,
    estimate_prompt_tokens,
    estimate_text_tokens,
)
from harborrag_engine.agent.tool_execution import MAX_TOOL_RESULT_CHARS, tool_definition


def _context(max_total_tokens: int) -> RunContext:
    options = AgentRunOptions(
        tenant_id="ACME",
        principal_id="reader-1",
        session_id="session-1",
        max_total_tokens=max_total_tokens,
    )
    return RunContext(
        identity=AgentRunIdentity("ACME", "reader-1", "session-1", "run-1", "reader-1"),
        conversation_identity=None,
        options=options,
        guard=ExecutionGuard(),
        events=None,
        current_user_message=None,
        created_at=None,  # type: ignore[arg-type]
    )


def _tool_round(call_id: str, result_chars: int) -> list[HarborChatMessage]:
    return [
        HarborChatMessage.assistant(
            None,
            tool_calls=(
                HarborToolCall(
                    id=call_id,
                    function=HarborToolCallFunction(
                        name="vector_search",
                        arguments='{"query":"x"}',
                        parsed_arguments={"query": "x"},
                    ),
                ),
            ),
        ),
        HarborChatMessage.tool("x" * result_chars, tool_call_id=call_id, name="vector_search"),
    ]


def test_estimate_text_tokens_amortizes_ascii_and_counts_non_ascii_per_char() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("a" * 3500) == 1000
    assert estimate_text_tokens("\u65e5\u672c\u8a9e") == 3
    assert estimate_text_tokens("ab\u00e9") == 2


def test_estimate_prompt_tokens_is_far_below_byte_length_for_large_tool_results() -> None:
    messages = [HarborChatMessage.user("question"), *_tool_round("call-1", MAX_TOOL_RESULT_CHARS)]
    estimate = estimate_prompt_tokens(messages, ())
    # ~16k chars must cost ~4.6k tokens, not one token per byte.
    assert MAX_TOOL_RESULT_CHARS / 4 < estimate < MAX_TOOL_RESULT_CHARS / 3


def test_second_retrieval_fits_a_32k_budget_after_two_full_tool_results() -> None:
    """The original defect: byte length as token count made the second
    retrieval turn raise TokenBudgetExhausted under a 32,768 budget as soon
    as a single 16k-char tool result was in the conversation."""
    tools = tuple(tool_definition(spec, False) for spec in Tools().specs)
    conversation = [
        HarborChatMessage.developer(_AGENT_INSTRUCTIONS),
        HarborChatMessage.user("question"),
        *_tool_round("call-1", MAX_TOOL_RESULT_CHARS),
        *_tool_round("call-2", MAX_TOOL_RESULT_CHARS),
    ]
    state = LoopState(
        conversation=conversation,
        executions=[],
        usage=HarborChatUsage(prompt_tokens=9_000, completion_tokens=200, total_tokens=9_200),
        step=3,
        version=4,
    )

    limit = completion_token_limit(_context(32_768), state, tools, reserve=SYNTHESIS_RESERVE_TOKENS)

    assert limit is not None
    assert limit > 8_000


def test_completion_token_limit_keeps_aggregate_ceiling_and_reserve() -> None:
    state = LoopState(
        conversation=[HarborChatMessage.user("a" * 350)],
        executions=[],
        usage=HarborChatUsage(prompt_tokens=50, completion_tokens=50, total_tokens=100),
        step=1,
        version=2,
    )
    prompt = estimate_prompt_tokens(state.conversation, ())

    assert completion_token_limit(_context(1_000), state, ()) == 1_000 - 100 - prompt
    assert completion_token_limit(_context(1_000), state, (), reserve=200) == 700 - prompt
    with pytest.raises(TokenBudgetExhausted):
        completion_token_limit(_context(1_000), state, (), minimum=1_000)
    with pytest.raises(TokenBudgetExhausted):
        completion_token_limit(_context(prompt + 100), state, ())
    assert completion_token_limit(_context(None), state, ()) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_budget_stop_still_runs_tool_free_synthesis_with_remaining_headroom() -> None:
    """A tool turn that no longer fits must not short-circuit to canned text
    when the (tool-free, smaller) synthesis turn still has >= the minimum
    completion headroom."""
    tools = Tools()
    tools.specs = [Spec("vector_search", description="d" * 2_000)]
    chat = Chat(
        [
            _response(call=("call-1", "vector_search", '{"query":"x"}')),
            _response(text="synthesized from evidence"),
        ]
    )
    conversation0 = [HarborChatMessage.developer(_AGENT_INSTRUCTIONS), HarborChatMessage.user("q")]
    definitions = tuple(tool_definition(spec, False) for spec in tools.specs)
    # Exactly enough for the first tool turn (cap of 1); after the tool round
    # grows the prompt, the second tool turn cannot fit but the synthesis
    # turn (no 2k-char tool schema, 640 reserved) can.
    budget = estimate_prompt_tokens(conversation0, definitions) + SYNTHESIS_RESERVE_TOKENS + 1
    runs = Runs()

    result = await AgentService(chat, tools, runs=runs).run(
        [HarborChatMessage.user("q")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="reader-1",
            session_id="session-1",
            max_steps=4,
            max_total_tokens=budget,
        ),
    )

    assert result.stop_reason is AgentStopReason.TOKEN_BUDGET_EXCEEDED
    assert result.response.text == "synthesized from evidence"
    assert result.turns == 2
    assert len(chat.requests) == 2
    synthesis = chat.requests[1]
    assert synthesis.tools == ()
    assert synthesis.max_completion_tokens is not None
    assert synthesis.max_completion_tokens >= 512
    assert "token budget" in synthesis.messages[-1].content
    persisted = runs.checkpoints[result.run_id]
    assert persisted.messages[-1].role is MessageRole.ASSISTANT
    assert persisted.messages[-1].content == "synthesized from evidence"


@pytest.mark.asyncio
async def test_budget_stop_falls_back_to_canned_text_only_when_synthesis_cannot_fit() -> None:
    chat = Chat([_response(text="must not be called")])

    result = await AgentService(chat, Tools()).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(
            tenant_id="ACME",
            principal_id="reader-1",
            session_id="session-1",
            max_total_tokens=300,
        ),
    )

    assert result.stop_reason is AgentStopReason.TOKEN_BUDGET_EXCEEDED
    assert "token budget was exhausted" in result.response.text
    assert chat.requests == []
