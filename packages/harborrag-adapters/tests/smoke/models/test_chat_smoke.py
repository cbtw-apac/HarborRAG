from __future__ import annotations

import pytest
from harborrag_adapters.models.chat import HarborChatClient
from harborrag_core.models.chat import HarborChatMessage

from ._config import chat_config


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_real_chat_completion_from_dotenv() -> None:
    async with HarborChatClient.from_config(chat_config()) as client:
        response = await client.achat(
            [
                HarborChatMessage.system("Return a brief plain-text answer."),
                HarborChatMessage.user("Reply with: harbor-chat-smoke-ok"),
            ]
        )

    assert response.text.strip()
    assert response.provider_model
    assert response.request_id
    assert response.latency_ms >= 0
