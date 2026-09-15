"""Structured output must report the provider usage it actually consumed."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from harborrag_core.models.capabilities import HarborChatCapabilities
from harborrag_core.models.chat import HarborChatMessage

from .chat_client_support import FakeInvocation, async_client, response_dict
from .test_structured import _configured

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


class TypedAnswer(BaseModel):
    answer: str
    confidence: float = Field(ge=0, le=1)


@pytest.mark.asyncio
async def test_structured_usage_sums_every_provider_call_including_repair(base_config) -> None:
    config = _configured(base_config, HarborChatCapabilities(structured_output=True), repairs=1)
    invocation = FakeInvocation(
        [
            response_dict(
                '{"answer":"missing confidence"}',
                usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            ),
            response_dict(
                '{"answer":"repaired","confidence":1.0}',
                usage={"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
            ),
        ]
    )

    result = await async_client(config, backend=invocation).achat_structured_usage(
        messages=[HarborChatMessage.user("question")],
        response_model=TypedAnswer,
    )

    assert result.value.answer == "repaired"
    assert result.provider_calls == 2
    assert result.usage.prompt_tokens == 10
    assert result.usage.completion_tokens == 7
    assert result.usage.total_tokens == 17
