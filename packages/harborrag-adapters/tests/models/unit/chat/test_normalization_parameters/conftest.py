from __future__ import annotations

from typing import Any

from pydantic import BaseModel, SecretStr

from harborrag_adapters.models.chat.configs import HarborChatProviderConfig
from harborrag_adapters.models.chat.registry import HarborProvider
from harborrag_core.models.capabilities import HarborChatCapabilities


class Answer(BaseModel):
    """Represent a minimal structured response schema."""

    answer: str


def deployment(**updates: Any) -> HarborChatProviderConfig:
    """Build a feature-rich OpenAI deployment for direct unit tests."""
    values: dict[str, Any] = {
        "name": "chat-a",
        "provider": HarborProvider.OPENAI,
        "model": "openai/gpt-test",
        "api_key": "secret",
        "headers": {"X-Deploy": SecretStr("one")},
        "capabilities": HarborChatCapabilities(
            multimodal=True,
            audio_input=True,
            structured_output=True,
            json_mode=True,
            tools=True,
            streaming=True,
        ),
    }
    values.update(updates)
    return HarborChatProviderConfig(**values)
