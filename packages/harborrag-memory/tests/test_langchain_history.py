from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from harborrag_core.ports.conversation import (
    ConversationIdentity,
    ConversationMessage,
    new_message_id,
)
from harborrag_memory.langchain import (
    HarborChatMessageHistory,
    from_langchain_message,
    to_langchain_message,
    to_langchain_messages,
)

pytestmark = [pytest.mark.unit]

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


@dataclass
class MessageStoreFake:
    values: dict[ConversationIdentity, list[ConversationMessage]] = field(default_factory=dict)

    async def append_messages(
        self, identity: ConversationIdentity, messages: Sequence[ConversationMessage]
    ) -> None:
        self.values.setdefault(identity, []).extend(messages)

    async def recent_messages(
        self, identity: ConversationIdentity, *, limit: int
    ) -> tuple[ConversationMessage, ...]:
        return tuple(self.values.get(identity, [])[-limit:])

    async def messages_after(
        self, identity: ConversationIdentity, *, after_message_id: str | None, limit: int
    ) -> tuple[ConversationMessage, ...]:
        rows = self.values.get(identity, [])
        if after_message_id is None:
            return tuple(rows[:limit])
        ids = [row.message_id for row in rows]
        if after_message_id not in ids:
            raise ValueError("unknown cursor")
        start = ids.index(after_message_id) + 1
        return tuple(rows[start : start + limit])

    async def clear_messages(self, identity: ConversationIdentity) -> None:
        self.values[identity] = []


@pytest.fixture
def identity() -> ConversationIdentity:
    return ConversationIdentity(
        tenant_id="tenant-1",
        principal_id="cred-1",
        session_id="s-1",
        user_id="user-1",
    )


@pytest.fixture
def store() -> MessageStoreFake:
    return MessageStoreFake()


def row(role: str, content: str, **extra: object) -> ConversationMessage:
    return ConversationMessage(
        message_id=new_message_id(),
        role=role,  # type: ignore[arg-type]
        content=content,
        created_at=NOW,
        **extra,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_history_round_trips_messages_through_store(store, identity) -> None:
    history = HarborChatMessageHistory(store, identity, run_id="run-1", clock=lambda: NOW)

    await history.aadd_messages(
        [
            SystemMessage(content="You are helpful"),
            HumanMessage(content="hello"),
            AIMessage(
                content="",
                tool_calls=[{"name": "search", "args": {"q": "x"}, "id": "call-1"}],
            ),
            ToolMessage(content="result", tool_call_id="call-1"),
            AIMessage(content="done"),
        ]
    )

    rows = store.values[identity]
    assert [r.role for r in rows] == ["system", "user", "assistant", "tool", "assistant"]
    assert all(r.run_id == "run-1" and r.created_at == NOW for r in rows)
    assert json.loads(rows[2].tool_calls_json or "[]")[0]["name"] == "search"
    assert rows[3].tool_call_id == "call-1"

    messages = await history.aget_messages()
    assert [type(m).__name__ for m in messages] == [
        "SystemMessage",
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]
    assistant = messages[2]
    assert isinstance(assistant, AIMessage)
    assert assistant.tool_calls[0]["args"] == {"q": "x"}
    assert assistant.tool_calls[0]["id"] == "call-1"
    tool = messages[3]
    assert isinstance(tool, ToolMessage)
    assert tool.tool_call_id == "call-1"
    assert [m.id for m in messages] == [r.message_id for r in rows]


@pytest.mark.asyncio
async def test_history_honours_recent_limit_and_clear(store, identity) -> None:
    history = HarborChatMessageHistory(store, identity, recent_limit=2)
    await history.aadd_messages([HumanMessage(content=str(i)) for i in range(5)])

    recent = await history.aget_messages()
    assert [m.text for m in recent] == ["3", "4"]

    await history.aclear()
    assert await history.aget_messages() == []


@pytest.mark.asyncio
async def test_empty_append_is_a_no_op(store, identity) -> None:
    history = HarborChatMessageHistory(store, identity)
    await history.aadd_messages([])
    assert identity not in store.values


def test_sync_methods_raise_clear_error(store, identity) -> None:
    history = HarborChatMessageHistory(store, identity)

    with pytest.raises(NotImplementedError, match="async-only"):
        _ = history.messages
    with pytest.raises(NotImplementedError, match="aadd_messages"):
        history.add_message(HumanMessage(content="hi"))
    with pytest.raises(NotImplementedError, match="aclear"):
        history.clear()
    with pytest.raises(ValueError, match="recent_limit"):
        HarborChatMessageHistory(store, identity, recent_limit=0)


def test_converters_preserve_metadata_and_accept_openai_tool_call_shape() -> None:
    stored = row(
        "assistant",
        "see sources",
        token_count=7,
        run_id="run-9",
        citations_json='[{"chunk_id": "c1"}]',
        tool_calls_json=json.dumps(
            [{"id": "call-2", "function": {"name": "lookup", "arguments": '{"k": 1}'}}]
        ),
    )

    message = to_langchain_message(stored)

    assert isinstance(message, AIMessage)
    assert message.tool_calls == [
        {"name": "lookup", "args": {"k": 1}, "id": "call-2", "type": "tool_call"}
    ]
    assert message.additional_kwargs["citations"] == [{"chunk_id": "c1"}]

    back = from_langchain_message(message)
    assert back.message_id == stored.message_id
    assert back.created_at == NOW
    assert back.token_count == 7
    assert back.run_id == "run-9"
    assert back.citations_json == '[{"chunk_id": "c1"}]'
    assert json.loads(back.tool_calls_json or "[]")[0]["args"] == {"k": 1}


def test_from_langchain_message_generates_identity_and_timestamp() -> None:
    message = from_langchain_message(HumanMessage(content="hi"), clock=lambda: NOW)

    assert message.message_id
    assert message.role == "user"
    assert message.created_at == NOW
    assert message.tool_calls_json is None


def test_unsupported_message_types_are_rejected() -> None:
    from langchain_core.messages import FunctionMessage

    with pytest.raises(TypeError, match="unsupported LangChain message type"):
        from_langchain_message(FunctionMessage(content="x", name="f"))
    assert to_langchain_messages([row("system", "s")])[0].text == "s"
