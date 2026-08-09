"""Hard aggregate token-budget calculations for agent model calls."""

from __future__ import annotations

import json

from harborrag_core.models.chat import HarborChatMessage, HarborChatResponse, HarborChatTool

from .loop_state import LoopState, RunContext


class TokenBudgetExhausted(RuntimeError):
    """Raised before a provider call that cannot fit the remaining hard budget."""


def completion_token_limit(
    context: RunContext,
    state: LoopState,
    tools: tuple[HarborChatTool, ...],
) -> int | None:
    """Return a provider completion cap that cannot exceed the run's budget."""
    budget = context.options.max_total_tokens
    if budget is None:
        return None
    remaining = budget - (state.usage.total_tokens or 0)
    payload = {
        "messages": [message.model_dump(mode="json") for message in state.conversation],
        "tools": [tool.model_dump(mode="json") for tool in tools],
    }
    # A tokenizer cannot emit more tokens than the UTF-8 byte representation
    # consumed by byte-fallback tokenizers. Reserving this conservative upper
    # bound makes the provider completion limit a hard aggregate ceiling.
    prompt_ceiling = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    limit = remaining - prompt_ceiling
    if limit < 1:
        raise TokenBudgetExhausted
    return limit


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


__all__ = ["TokenBudgetExhausted", "completion_token_limit", "exhausted_response"]
