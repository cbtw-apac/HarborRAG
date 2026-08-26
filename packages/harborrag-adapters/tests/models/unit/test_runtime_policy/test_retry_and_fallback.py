from __future__ import annotations

import pytest
from chat.chat_client_support import async_client, sync_client
from pydantic import ValidationError

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_core.models.chat import HarborChatMessage
from harborrag_core.models.errors import (
    HarborChatAuthenticationError,
    HarborChatRateLimitError,
    HarborChatTimeoutError,
)

from .fakes import Invocation, response, runtime_config

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_same_deployment_retry_is_distinct_from_fallback() -> None:
    invocation = Invocation([HarborChatRateLimitError("limited"), response()])

    result = sync_client(runtime_config(attempts=2), backend=invocation).chat(
        [HarborChatMessage.user("hello")]
    )

    assert result.retry_count == 1
    assert result.fallback_count == 0
    assert result.provider_metadata["routing"] == {
        "same_deployment_retries": 1,
        "deployment_failovers": 0,
        "model_fallbacks": 0,
    }


def test_non_retryable_failure_stops_immediately() -> None:
    invocation = Invocation([HarborChatAuthenticationError("bad credentials")])

    with pytest.raises(HarborChatAuthenticationError):
        sync_client(runtime_config(attempts=3), backend=invocation).chat(
            [HarborChatMessage.user("hello")]
        )

    assert len(invocation.calls) == 1


def test_deployment_failover_is_reported_separately() -> None:
    invocation = Invocation([TimeoutError("slow"), response()])

    result = sync_client(runtime_config(deployments=2), backend=invocation).chat(
        [HarborChatMessage.user("hello")]
    )

    assert [call["model"] for call in invocation.calls] == [
        "openai/model-0",
        "openai/model-1",
    ]
    assert result.deployment == "primary-1"
    assert result.provider_metadata["routing"]["deployment_failovers"] == 1
    assert result.provider_metadata["routing"]["model_fallbacks"] == 0


@pytest.mark.asyncio
async def test_async_logical_model_fallback() -> None:
    invocation = Invocation([TimeoutError("slow"), response("fallback")])

    result = await async_client(runtime_config(fallback=True), backend=invocation).achat(
        [HarborChatMessage.user("hello")]
    )

    assert result.logical_model == "secondary"
    assert result.text == "fallback"
    assert result.provider_metadata["routing"]["model_fallbacks"] == 1


def test_maximum_retry_exhaustion_preserves_timeout_error() -> None:
    invocation = Invocation([TimeoutError("slow"), TimeoutError("still slow")])

    with pytest.raises(HarborChatTimeoutError):
        sync_client(runtime_config(attempts=2), backend=invocation).chat(
            [HarborChatMessage.user("hello")]
        )

    assert len(invocation.calls) == 2


def test_circular_fallback_graph_is_rejected() -> None:
    with pytest.raises(ValidationError, match="circular chat fallback chain"):
        HarborChatClientConfig.from_dict(
            {
                "default_model": "a",
                "models": {
                    "a": {
                        "fallbacks": ["b"],
                        "provider": "openai",
                        "model": "openai/a",
                        "api_key": "key",
                    },
                    "b": {
                        "fallbacks": ["a"],
                        "provider": "openai",
                        "model": "openai/b",
                        "api_key": "key",
                    },
                },
            }
        )
