from datetime import UTC, datetime

from harborrag_core.ports.conversation import (
    ConversationMessage,
    ConversationTurn,
    turns_from_messages,
)


def test_completed_turns_skip_partial_and_tool_call_answers() -> None:
    now = datetime.now(UTC)
    messages = (
        ConversationMessage("u", "user", "question", now),
        ConversationMessage("call", "assistant", "searching", now, tool_calls_json="[{}]"),
        ConversationMessage("tool", "tool", "evidence", now),
        ConversationMessage("partial", "assistant", "unfinished", now, partial=True),
        ConversationMessage("final", "assistant", "answer", now),
        ConversationMessage("next", "user", "next question", now),
        ConversationMessage("failed", "assistant", "interrupted", now, partial=True),
    )
    assert turns_from_messages(messages) == (ConversationTurn("question", "answer"),)
