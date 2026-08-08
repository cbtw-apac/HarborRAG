"""Small pure helpers shared across the agent loop."""

from __future__ import annotations

from collections.abc import Sequence

from harborrag_core.models.chat import HarborChatMessage, HarborChatUsage, MessageRole
from harborrag_engine.conversation import ConversationTurn

from .schemas import AgentRunOptions


def validate_options(options: AgentRunOptions) -> None:
    if not 1 <= options.max_steps <= 8:
        raise ValueError("agent max_steps must be between 1 and 8")
    if options.timeout_seconds is not None and options.timeout_seconds <= 0:
        raise ValueError("agent timeout_seconds must be positive")
    if options.max_repeated_tool_calls < 1:
        raise ValueError("agent max_repeated_tool_calls must be at least 1")
    if options.synthesis_timeout_seconds is not None and options.synthesis_timeout_seconds <= 0:
        raise ValueError("agent synthesis_timeout_seconds must be positive")
    if options.max_total_tokens is not None and options.max_total_tokens < 1:
        raise ValueError("agent max_total_tokens must be at least 1")


def add_usage(left: HarborChatUsage, right: HarborChatUsage) -> HarborChatUsage:
    def optional_sum(name: str) -> int | None:
        first = getattr(left, name)
        second = getattr(right, name)
        return None if first is None and second is None else (first or 0) + (second or 0)

    return HarborChatUsage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
        cache_read_input_tokens=optional_sum("cache_read_input_tokens"),
        cache_creation_input_tokens=optional_sum("cache_creation_input_tokens"),
        reasoning_tokens=optional_sum("reasoning_tokens"),
    )


def turn_messages(turns: Sequence[ConversationTurn]) -> list[HarborChatMessage]:
    messages: list[HarborChatMessage] = []
    for turn in turns:
        messages.extend(
            (
                HarborChatMessage.user(turn.user_content),
                HarborChatMessage.assistant(turn.assistant_content),
            )
        )
    return messages


def last_user_message(messages: Sequence[HarborChatMessage]) -> HarborChatMessage | None:
    return next(
        (message for message in reversed(messages) if message.role is MessageRole.USER),
        None,
    )


__all__ = ["add_usage", "last_user_message", "turn_messages", "validate_options"]
