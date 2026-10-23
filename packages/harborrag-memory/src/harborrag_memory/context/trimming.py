"""Token accounting and budget trimming for the verbatim replay window."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from langchain_core.messages import BaseMessage, trim_messages

from harborrag_core.ports.conversation import ConversationMessage

from ..langchain.converters import to_langchain_messages

type TokenCounter = Callable[[str], int]

logger = logging.getLogger(__name__)


def message_tokens(message: ConversationMessage, counter: TokenCounter) -> int:
    """Return a message's persisted token count, or an estimate of its text."""

    if message.token_count is not None:
        return message.token_count
    return counter(message.content)


def total_tokens(messages: Sequence[ConversationMessage], counter: TokenCounter) -> int:
    """Return the token cost of ``messages`` taken together."""

    return sum(message_tokens(message, counter) for message in messages)


def keep_boundary(messages: Sequence[ConversationMessage], keep: int) -> int:
    """Index where a ``keep``-message verbatim tail should start.

    A raw ``messages[-keep:]`` slice can begin on a tool result or on the tail
    of an assistant tool call, which the token trim then has to drop -- often
    collapsing the window to a single message. Walking the boundary back to the
    nearest user turn keeps the tool call and its results together. When no
    user turn precedes the tail, the plain slice stands so summarization still
    makes progress.
    """

    start = max(0, len(messages) - keep)
    index = start
    while index > 0 and messages[index].role != "user":
        index -= 1
    if messages and messages[index].role != "user":
        return start
    return index


def trim_window(
    messages: Sequence[ConversationMessage],
    *,
    max_tokens: int,
    counter: TokenCounter,
) -> tuple[ConversationMessage, ...]:
    """Trim ``messages`` to ``max_tokens``, keeping the newest, oldest-first.

    LangChain's ``trim_messages`` does the work so the window can never begin
    on a dangling tool result or on the tail of an assistant tool call: with
    ``start_on="human"`` the survivors always start at a user message. The
    survivors are mapped back to the persisted rows by message id, and a
    budget too small for even one message still keeps the newest message.
    """

    if not messages:
        return ()
    rows = {message.message_id: message for message in messages}
    try:
        kept = trim_messages(
            to_langchain_messages(messages),
            max_tokens=max_tokens,
            token_counter=_counter_over(rows, counter),
            strategy="last",
            start_on="human",
            include_system=False,
            allow_partial=False,
        )
        survivors = tuple(rows[kept_message.id] for kept_message in kept if kept_message.id in rows)
    except Exception:
        logger.warning("token trimming failed; falling back to a tail window", exc_info=True)
        survivors = _tail_within_budget(messages, max_tokens=max_tokens, counter=counter)
    return survivors or (messages[-1],)


def _counter_over(
    rows: dict[str, ConversationMessage],
    counter: TokenCounter,
) -> Callable[[list[BaseMessage]], int]:
    def count(messages: list[BaseMessage]) -> int:
        total = 0
        for message in messages:
            row = rows.get(message.id or "")
            total += message_tokens(row, counter) if row is not None else counter(str(message.text))
        return total

    return count


def _tail_within_budget(
    messages: Sequence[ConversationMessage],
    *,
    max_tokens: int,
    counter: TokenCounter,
) -> tuple[ConversationMessage, ...]:
    kept: list[ConversationMessage] = []
    used = 0
    for message in reversed(messages):
        cost = message_tokens(message, counter)
        if kept and used + cost > max_tokens:
            break
        kept.append(message)
        used += cost
    while len(kept) > 1 and kept[-1].role != "user":
        kept.pop()
    return tuple(reversed(kept))


__all__ = [
    "TokenCounter",
    "keep_boundary",
    "message_tokens",
    "total_tokens",
    "trim_window",
]
