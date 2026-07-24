from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from model_runtime_support import chat_config, embed_config

from harborrag_adapters.models.chat.validation import validate_chat_configuration
from harborrag_adapters.models.embed.validation import validate_embed_configuration
from harborrag_adapters.models.runtime.config import (
    RoutingConfig,
    RoutingEngine,
    RoutingStrategy,
)
from harborrag_core.models.errors import (
    HarborChatConfigurationError,
    HarborEmbedConfigurationError,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest.mark.parametrize(
    ("config", "validator", "error_type"),
    [
        (
            chat_config(
                routing=RoutingConfig(
                    engine=RoutingEngine.LITELLM_ROUTER,
                    strategy=RoutingStrategy.ROUND_ROBIN,
                )
            ),
            validate_chat_configuration,
            HarborChatConfigurationError,
        ),
        (
            embed_config().model_copy(
                update={
                    "routing": RoutingConfig(
                        engine=RoutingEngine.LITELLM_ROUTER,
                        strategy=RoutingStrategy.ROUND_ROBIN,
                    )
                }
            ),
            validate_embed_configuration,
            HarborEmbedConfigurationError,
        ),
    ],
)
def test_litellm_router_rejects_exact_round_robin(
    config: Any,
    validator: Callable[[Any], None],
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type, match="round-robin"):
        validator(config)
