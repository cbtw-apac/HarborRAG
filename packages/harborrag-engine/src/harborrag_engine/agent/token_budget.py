"""Hard aggregate token-budget calculations for agent model calls."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable

from harborrag_core.models.chat import HarborChatMessage, HarborChatResponse, HarborChatTool

from .loop_state import LoopState, RunContext

# Completion headroom the tool-enabled turns leave untouched so the final
# tool-free synthesis turn can still run after a budget stop, and the smallest
# completion cap that synthesis turn is allowed to run with.
MIN_SYNTHESIS_COMPLETION_TOKENS = 512
# What a tool-enabled turn holds back: the synthesis completion minimum plus
# room for the developer instruction that turn prepends to the conversation.
SYNTHESIS_RESERVE_TOKENS = MIN_SYNTHESIS_COMPLETION_TOKENS + 128
# Typical BPE tokenizers average ~4 ASCII characters per token on English prose
# and more on JSON/code; 3.5 over-counts by a safe margin without collapsing
# to the bytes-as-tokens bound that made a 16k-char tool result cost 16k
# "tokens" of budget.
_ASCII_CHARS_PER_TOKEN = 3.5
# Role/name/separator framing each provider adds around a message.
_PER_MESSAGE_OVERHEAD_TOKENS = 4
# Image/audio parts are billed by the provider independent of their text
# length; this is a conservative flat charge (a high-detail image is ~1.5k).
_NON_TEXT_PART_TOKENS = 1536


class TokenBudgetExhausted(RuntimeError):
    """Raised before a provider call that cannot fit the remaining hard budget."""


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens for ``text``: ASCII amortized, every non-ASCII char one token."""

    if not text:
        return 0
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    return math.ceil(ascii_chars / _ASCII_CHARS_PER_TOKEN) + (len(text) - ascii_chars)


def _message_tokens(message: HarborChatMessage) -> int:
    tokens = _PER_MESSAGE_OVERHEAD_TOKENS
    if message.name:
        tokens += estimate_text_tokens(message.name)
    content = message.content
    if isinstance(content, str):
        tokens += estimate_text_tokens(content)
    elif content is not None:
        for part in content:
            text = getattr(part, "text", None)
            tokens += estimate_text_tokens(text) if isinstance(text, str) else _NON_TEXT_PART_TOKENS
    for call in message.tool_calls:
        tokens += _PER_MESSAGE_OVERHEAD_TOKENS
        tokens += estimate_text_tokens(call.function.name)
        tokens += estimate_text_tokens(call.function.arguments)
    return tokens


def estimate_prompt_tokens(
    messages: Iterable[HarborChatMessage],
    tools: Iterable[HarborChatTool],
) -> int:
    """Conservative token estimate for one request's prompt side.

    Message text, tool-call arguments, and the JSON tool definitions are
    estimated with :func:`estimate_text_tokens`; each message (and each tool
    call) adds a small fixed framing overhead. This intentionally over-counts
    a little so the aggregate ceiling stays hard, but it no longer charges
    one token per UTF-8 byte of the serialized conversation.
    """

    tokens = sum(_message_tokens(message) for message in messages)
    for tool in tools:
        tokens += _PER_MESSAGE_OVERHEAD_TOKENS
        tokens += estimate_text_tokens(
            json.dumps(tool.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        )
    return tokens


def completion_token_limit(
    context: RunContext,
    state: LoopState,
    tools: tuple[HarborChatTool, ...],
    *,
    reserve: int = 0,
    minimum: int = 1,
) -> int | None:
    """Return a provider completion cap that cannot exceed the run's budget.

    The cap is ``budget - spent - estimated_prompt - reserve``: whatever the
    next call may emit while keeping the run's total under ``max_total_tokens``
    (aggregate ceiling), less ``reserve`` tokens held back for a later call.
    Raises :class:`TokenBudgetExhausted` when the cap would fall below
    ``minimum``.
    """
    budget = context.options.max_total_tokens
    if budget is None:
        return None
    remaining = budget - (state.usage.total_tokens or 0)
    limit = remaining - estimate_prompt_tokens(state.conversation, tools) - reserve
    if limit < max(minimum, 1):
        raise TokenBudgetExhausted
    return limit


def over_token_budget(context: RunContext, state: LoopState) -> bool:
    """Cap the run's own accumulated usage, independent of per-call limits.

    Per-call caps (``MAX_TOOL_CALLS_PER_TURN`` x ``MAX_TOOL_RESULT_CHARS``,
    ``max_steps``) are each individually bounded, but nothing else checks
    their sum -- a full-width run could still accumulate several million
    characters of resent conversation and tool output. This is the backstop
    on the aggregate, checked once per completed step.
    """

    budget = context.options.max_total_tokens
    return budget is not None and (state.usage.total_tokens or 0) >= budget


def exhausted_response(run_id: str, message: str) -> HarborChatResponse:
    """Build a deterministic local response without spending more model tokens."""
    return HarborChatResponse(
        id=f"{run_id}:token-budget",
        logical_model="harborrag-agent",
        provider="local",
        provider_model="token-budget-guard",
        deployment="local",
        message=HarborChatMessage.assistant(message),
        finish_reason="length",
    )


__all__ = [
    "MIN_SYNTHESIS_COMPLETION_TOKENS",
    "SYNTHESIS_RESERVE_TOKENS",
    "TokenBudgetExhausted",
    "completion_token_limit",
    "estimate_prompt_tokens",
    "estimate_text_tokens",
    "exhausted_response",
    "over_token_budget",
]
