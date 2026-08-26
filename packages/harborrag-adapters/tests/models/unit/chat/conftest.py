from __future__ import annotations

import pytest

from harborrag_adapters.models.chat import HarborChatClientConfig


@pytest.fixture
def base_config() -> HarborChatClientConfig:
    return HarborChatClientConfig.from_dict(
        {
            "default_model": "primary",
            "timeouts": {"request_seconds": 17},
            "models": {
                "primary": {
                    "aliases": ["default-chat"],
                    "default_params": {"temperature": 0.25},
                    "provider": "openai",
                    "model": "openai/gpt-4o-mini",
                    "api_key": "test-key",
                }
            },
        }
    )
