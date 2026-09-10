"""Build the per-message history records one agent run leaves behind."""

from __future__ import annotations

from uuid import uuid4

from harborrag_core.base import utc_now
from harborrag_core.ports.conversation import ConversationMessage


def new_message_id() -> str:
    """Generate one opaque conversation-message identifier."""

    return f"msg-{uuid4().hex}"


def run_exchange_messages(
    user_content: str,
    assistant_content: str,
    *,
    run_id: str,
    completion_tokens: int | None = None,
) -> tuple[ConversationMessage, ConversationMessage]:
    """The user question and final answer of one run, both tagged with ``run_id``.

    The final answer carries no ``tool_calls_json``: intermediate tool traffic
    is checkpointed with the run, not replayed as conversation history.
    """

    created_at = utc_now()
    user = ConversationMessage(
        message_id=new_message_id(),
        role="user",
        content=user_content,
        created_at=created_at,
        run_id=run_id,
    )
    assistant = ConversationMessage(
        message_id=new_message_id(),
        role="assistant",
        content=assistant_content,
        created_at=created_at,
        token_count=completion_tokens,
        run_id=run_id,
    )
    return user, assistant


__all__ = ["new_message_id", "run_exchange_messages"]
