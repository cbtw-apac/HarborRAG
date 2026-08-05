"""Contract tests for chat through MCP."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatResponse,
    HarborChatUsage,
)
from harborrag_mcp_server.tools.chat import ChatTool
from harborrag_runtime.chat import ChatPrompt


class _ChatFacade:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.request = None
        self.requests = []
        self.prompt = None

    async def complete(self, request, *, prompt=None):
        if self.failure is not None:
            raise self.failure
        self.request = request
        self.requests.append(request)
        self.prompt = prompt
        return HarborChatResponse(
            id="chat-1",
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="private-deployment",
            message=HarborChatMessage.assistant("MCP response"),
            finish_reason="stop",
            usage=HarborChatUsage(
                prompt_tokens=4,
                completion_tokens=2,
                total_tokens=6,
            ),
            provider_metadata={"private": "not public"},
        )


@pytest.mark.asyncio
async def test_chat_tool_propagates_identity_prompt_and_controls() -> None:
    facade = _ChatFacade()
    tool = ChatTool(runtime=SimpleNamespace(chat=facade))

    result = await tool.call(
        {
            "message": "Explain HarborRAG",
            "tenant_id": "ACME",
            "system": "Use plain language.",
            "prompt": "concise",
            "model": "primary",
            "temperature": 0.1,
            "max_tokens": 200,
            "session_id": "session-1",
        },
        principal_id="mcp-user",
    )

    assert result["ok"] is True
    assert result["message"] == "MCP response"
    assert result["usage"]["total_tokens"] == 6
    assert "deployment" not in result
    assert "provider_metadata" not in result
    assert facade.prompt is ChatPrompt.CONCISE
    assert facade.request.metadata.tenant_id == "ACME"
    assert facade.request.metadata.user_id is None
    assert facade.request.metadata.conversation_id == "session-1"
    assert facade.request.logical_model == "primary"
    assert [message.role.value for message in facade.request.messages] == ["system", "user"]


@pytest.mark.asyncio
async def test_chat_tool_generates_session_when_omitted() -> None:
    facade = _ChatFacade()
    tool = ChatTool(runtime=SimpleNamespace(chat=facade))

    result = await tool.call(
        {"message": "Hello", "tenant_id": "ACME"},
        principal_id="mcp-user",
    )

    assert result["ok"] is True
    assert result["session_id"].startswith("session-")
    assert facade.request.metadata.conversation_id == result["session_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"message": "", "tenant_id": "ACME"},
        {"message": "Hello", "tenant_id": "ACME", "prompt": "unknown"},
        {"message": "Hello", "tenant_id": "ACME", "temperature": True},
        {"message": "Hello", "tenant_id": "ACME", "max_tokens": 0},
    ],
)
async def test_chat_tool_rejects_invalid_direct_inputs(arguments) -> None:
    tool = ChatTool(runtime=SimpleNamespace(chat=_ChatFacade()))

    assert (await tool.call(arguments, principal_id="mcp-user"))["ok"] is False


@pytest.mark.asyncio
async def test_chat_tool_hides_provider_failures() -> None:
    tool = ChatTool(
        runtime=SimpleNamespace(chat=_ChatFacade(RuntimeError("private provider response")))
    )

    result = await tool.call(
        {
            "message": "Hello",
            "tenant_id": "ACME",
            "session_id": "session-1",
        },
        principal_id="mcp-user",
    )

    assert result == {"ok": False, "error": "chat backend failed"}


@pytest.mark.asyncio
async def test_chat_tool_recalls_user_session_history() -> None:
    facade = _ChatFacade()
    tool = ChatTool(runtime=SimpleNamespace(chat=facade))
    identity = {
        "tenant_id": "ACME",
        "session_id": "session-1",
    }

    await tool.call({"message": "First", **identity}, principal_id="mcp-user")
    await tool.call({"message": "Second", **identity}, principal_id="mcp-user")

    second = facade.requests[1]
    assert [message.content for message in second.messages] == [
        "First",
        "MCP response",
        "Second",
    ]
    assert second.metadata.user_id is None
    assert second.metadata.conversation_id == "session-1"
