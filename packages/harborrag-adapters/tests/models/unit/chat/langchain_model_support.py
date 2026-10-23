"""Shared models and helpers for HarborChatModel LangChain tests."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.chat.langchain import HarborChatModel

from .chat_client_support import FakeInvocation, async_client, response_dict


class Weather(BaseModel):
    """Look up the weather for a city."""

    city: str = Field(description="City name")


class Summary(BaseModel):
    title: str
    score: int


def make_model(
    config: HarborChatClientConfig, invocation: FakeInvocation, **kwargs: Any
) -> HarborChatModel:
    return HarborChatModel(
        async_client(config, backend=invocation),
        logical_model="primary",
        request_metadata={"tenant_id": "tenant-1", "user_id": "user-1"},
        **kwargs,
    )


def tool_call_response(arguments: str = '{"city":"Paris"}') -> dict[str, Any]:
    raw = response_dict(None, finish_reason="tool_calls")
    raw["choices"][0]["message"]["tool_calls"] = [
        {
            "id": "call-weather",
            "type": "function",
            "index": 0,
            "function": {"name": "Weather", "arguments": arguments},
        }
    ]
    return raw
