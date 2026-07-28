from __future__ import annotations

import pytest
from model_runtime_support import chat_config

from harborrag_adapters.models.chat.validation import validate_chat_request
from harborrag_core.models.capabilities import HarborChatCapabilities
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    HarborTokenBudget,
    ImageURLContentPart,
    InputAudioContentPart,
    MessageRole,
)
from harborrag_core.models.errors import (
    HarborChatCapabilityError,
    HarborChatInvalidRequestError,
)

from .conftest import deployment

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_chat_request_capability_and_security_validation() -> None:
    base = deployment(capabilities=HarborChatCapabilities(streaming=True))
    config = chat_config(deployments=(base,))
    requests = [
        HarborChatRequest(messages=(HarborChatMessage.user("x"),), reasoning_effort="high"),
        HarborChatRequest(
            messages=(HarborChatMessage.user("x"),),
            token_budget=HarborTokenBudget(max_input_tokens=10),
        ),
        HarborChatRequest(messages=(HarborChatMessage.user("x"),), tool_choice="auto"),
        HarborChatRequest(messages=(HarborChatMessage.user("x"),), parallel_tool_calls=True),
        HarborChatRequest(messages=(HarborChatMessage(role=MessageRole.TOOL, content="x"),)),
        HarborChatRequest(
            messages=(HarborChatMessage.user("x"),),
            response_format={"type": "json_object"},
        ),
        HarborChatRequest(messages=(HarborChatMessage.user("x"),), extra_params={"model": "bad"}),
        HarborChatRequest(
            messages=(HarborChatMessage.user("x"),),
            custom_headers={"Authorization": "bad"},
        ),
    ]
    for request in requests:
        with pytest.raises((HarborChatCapabilityError, HarborChatInvalidRequestError)):
            validate_chat_request(request, config, base)
    multimodal = HarborChatRequest(
        messages=(
            HarborChatMessage(
                role=MessageRole.USER,
                content=(ImageURLContentPart(image_url={"url": "https://example.test/image.png"}),),
            ),
        )
    )
    with pytest.raises(HarborChatCapabilityError):
        validate_chat_request(multimodal, config, base)
    audio_only = deployment(capabilities=HarborChatCapabilities(multimodal=True, audio_input=False))
    audio_request = HarborChatRequest(
        messages=(
            HarborChatMessage(
                role=MessageRole.USER,
                content=(InputAudioContentPart(input_audio={"data": "eA==", "format": "wav"}),),
            ),
        )
    )
    with pytest.raises(HarborChatCapabilityError):
        validate_chat_request(audio_request, chat_config(deployments=(audio_only,)), audio_only)
