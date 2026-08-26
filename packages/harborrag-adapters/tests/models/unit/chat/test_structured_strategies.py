from __future__ import annotations

from typing import Any

import pytest
from model_runtime_support import FakeChatInvocation, chat_config
from pydantic import BaseModel

from harborrag_adapters.models.chat.configs import HarborChatProviderConfig
from harborrag_adapters.models.chat.registry import HarborProvider
from harborrag_adapters.models.chat.structured_strategy import StructuredOutputStrategy
from harborrag_core.models.capabilities import HarborChatCapabilities
from harborrag_core.models.chat import HarborChatMessage
from harborrag_core.models.errors import HarborChatCapabilityError

from .chat_client_support import sync_client
from .test_client_execution import raw_chat

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


class Answer(BaseModel):
    answer: str


def test_explicit_native_schema_strategy_uses_json_schema() -> None:
    invocation = FakeChatInvocation([raw_chat('{"answer":"native"}')])
    client = sync_client(chat_config(), backend=invocation)

    result = client.chat_structured(
        [HarborChatMessage.user("answer")],
        response_model=Answer,
        strategy=StructuredOutputStrategy.NATIVE_SCHEMA,
    )

    assert result.answer == "native"
    assert invocation.calls[0]["response_format"]["type"] == "json_schema"


def test_explicit_json_mode_strategy_uses_json_object() -> None:
    invocation = FakeChatInvocation([raw_chat('{"answer":"json"}')])
    client = sync_client(chat_config(), backend=invocation)

    result = client.chat_structured(
        [HarborChatMessage.user("answer")],
        response_model=Answer,
        strategy=StructuredOutputStrategy.JSON_MODE,
    )

    assert result.answer == "json"
    assert invocation.calls[0]["response_format"] == {"type": "json_object"}


def test_explicit_prompt_fallback_injects_schema_instruction() -> None:
    invocation = FakeChatInvocation([raw_chat('{"answer":"prompt"}')])
    client = sync_client(chat_config(), backend=invocation)

    result = client.chat_structured(
        [HarborChatMessage.user("answer")],
        response_model=Answer,
        strategy=StructuredOutputStrategy.PROMPT_FALLBACK,
    )

    assert result.answer == "prompt"
    assert invocation.calls[0].get("response_format") is None
    assert "JSON Schema" in invocation.calls[0]["messages"][0]["content"]


def test_explicit_native_strategy_rejects_unsupported_deployment() -> None:
    deployment = HarborChatProviderConfig(
        name="plain",
        provider=HarborProvider.OPENAI,
        model="openai/plain",
        api_key="secret",
        capabilities=HarborChatCapabilities(streaming=True),
    )
    client = sync_client(
        chat_config(deployments=(deployment,)),
        backend=FakeChatInvocation([]),
    )

    with pytest.raises(HarborChatCapabilityError):
        client.chat_structured(
            [HarborChatMessage.user("answer")],
            response_model=Answer,
            strategy=StructuredOutputStrategy.NATIVE_SCHEMA,
        )


def test_complete_response_normalizes_reasoning_content() -> None:
    raw: dict[str, Any] = raw_chat("answer")
    raw["choices"][0]["message"]["reasoning_content"] = "private chain summary"
    client = sync_client(chat_config(), backend=FakeChatInvocation([raw]))

    response = client.chat([HarborChatMessage.user("reason")])

    assert response.reasoning_content == "private chain summary"
