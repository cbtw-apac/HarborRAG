from __future__ import annotations

import pytest
from harborrag_adapters.models.chat import (
    HarborChatClient,
    HarborChatClientConfig,
)
from harborrag_adapters.models.common.config import RoutingEngine
from harborrag_core.models.chat import (
    FinishReason,
    HarborChatMessage,
    HarborChatRequest,
)
from harborrag_core.models.errors import (
    HarborChatAuthenticationError,
    HarborChatCapabilityError,
    HarborChatConfigurationError,
    HarborChatConnectionError,
    HarborChatInvalidRequestError,
    HarborChatProviderError,
    HarborChatRateLimitError,
    HarborChatTimeoutError,
)
from harborrag_core.models.protocols import (
    AsyncHarborChatClientProtocol,
    HarborChatClientProtocol,
)

from .chat_client_support import FakeInvocation, response_dict

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_sync_completion_normalizes_messages_and_response(base_config) -> None:
    invocation = FakeInvocation([response_dict("sync")])
    client = HarborChatClient(base_config, invocation=invocation)

    response = client.chat([{"role": "user", "content": "hello"}])

    assert response.text == "sync"
    assert response.finish_reason is FinishReason.STOP
    assert response.usage.total_tokens == 5
    assert response.logical_model == "primary"
    assert response.request_id
    assert invocation.calls[0]["messages"] == [{"role": "user", "content": "hello"}]
    assert invocation.calls[0]["temperature"] == 0.25
    assert isinstance(client, HarborChatClientProtocol)


@pytest.mark.asyncio
async def test_async_completion_uses_same_client_and_contract(base_config) -> None:
    invocation = FakeInvocation([response_dict("async")])
    client = HarborChatClient(base_config, invocation=invocation)

    response = await client.achat([HarborChatMessage.user("hello")], temperature=0.6)

    assert response.text == "async"
    assert invocation.async_calls[0]["temperature"] == 0.6
    assert isinstance(client, AsyncHarborChatClientProtocol)


def test_alias_resolution_and_timeout_forwarding(base_config) -> None:
    invocation = FakeInvocation([response_dict()])
    client = HarborChatClient(base_config, invocation=invocation)

    response = client.chat(
        [HarborChatMessage.user("hello")],
        model="default-chat",
        max_tokens=42,
        stop=("done",),
    )

    call = invocation.calls[0]
    assert response.logical_model == "primary"
    assert call["timeout"] == 17
    assert call["max_tokens"] == 42
    assert call["stop"] == ["done"]
    assert call["stream"] is False


def test_request_object_preserves_identity_metadata(base_config) -> None:
    invocation = FakeInvocation([response_dict()])
    request = HarborChatRequest(
        messages=(HarborChatMessage.user("hello"),),
        logical_model="primary",
        metadata={"request_id": "req-123", "user_id": "user-1"},
    )

    response = HarborChatClient(base_config, invocation=invocation).chat(request=request)

    assert response.request_id == "req-123"
    assert invocation.calls[0]["user"] == "user-1"


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        ("end_turn", FinishReason.STOP),
        ("max_output_tokens", FinishReason.LENGTH),
        ("safety", FinishReason.CONTENT_FILTER),
        ("provider-specific", FinishReason.UNKNOWN),
        (None, FinishReason.UNKNOWN),
    ],
)
def test_finish_reason_normalization(base_config, finish_reason, expected) -> None:
    raw = response_dict(finish_reason=finish_reason or "stop")
    raw["choices"][0]["finish_reason"] = finish_reason

    response = HarborChatClient(base_config, invocation=FakeInvocation([raw])).chat(
        [HarborChatMessage.user("hello")]
    )

    assert response.finish_reason is expected


def test_usage_normalizes_input_output_and_detail_tokens(base_config) -> None:
    usage = {
        "input_tokens": 9,
        "output_tokens": 4,
        "prompt_tokens_details": {"cached_tokens": 3},
        "completion_tokens_details": {"reasoning_tokens": 2},
    }
    client = HarborChatClient(base_config, invocation=FakeInvocation([response_dict(usage=usage)]))

    response = client.chat([HarborChatMessage.user("hello")])

    assert response.usage.prompt_tokens == 9
    assert response.usage.completion_tokens == 4
    assert response.usage.total_tokens == 13
    assert response.usage.cache_read_input_tokens == 3
    assert response.usage.reasoning_tokens == 2


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"id": "bad", "choices": []},
        {"id": "bad", "choices": [{}]},
        {"id": "bad", "choices": [{"message": {"role": "user", "content": "x"}}]},
        {
            "id": "bad",
            "choices": [{"message": {"role": "assistant", "content": ["x"]}}],
        },
        {
            "id": "bad",
            "choices": [{"message": {"role": "assistant", "content": "x"}}],
            "usage": {"prompt_tokens": -1},
        },
    ],
)
def test_malformed_provider_responses_are_typed(base_config, raw) -> None:
    client = HarborChatClient(base_config, invocation=FakeInvocation([raw]))

    with pytest.raises(HarborChatProviderError, match="malformed"):
        client.chat([HarborChatMessage.user("hello")])


@pytest.mark.parametrize(
    ("exception_name", "expected_error"),
    [
        ("AuthenticationError", HarborChatAuthenticationError),
        ("RateLimitError", HarborChatRateLimitError),
    ],
)
def test_litellm_errors_are_mapped(
    base_config, monkeypatch, exception_name, expected_error
) -> None:
    import litellm

    class ProviderFailure(Exception):
        pass

    monkeypatch.setattr(litellm, exception_name, ProviderFailure)
    client = HarborChatClient(
        base_config,
        invocation=FakeInvocation(
            [ProviderFailure("provider failed"), ProviderFailure("provider failed")]
        ),
    )

    with pytest.raises(expected_error) as captured:
        client.chat([HarborChatMessage.user("hello")])

    assert captured.value.provider == "openai"
    assert captured.value.logical_model == "primary"
    assert captured.value.request_id


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        (TimeoutError("slow"), HarborChatTimeoutError),
        (ConnectionError("offline"), HarborChatConnectionError),
    ],
)
def test_transport_errors_are_mapped(base_config, failure, expected_error) -> None:
    client = HarborChatClient(base_config, invocation=FakeInvocation([failure, failure]))

    with pytest.raises(expected_error):
        client.chat([HarborChatMessage.user("hello")])


def test_undeclared_json_mode_fails_before_invocation(base_config) -> None:
    invocation = FakeInvocation([response_dict()])
    client = HarborChatClient(base_config, invocation=invocation)

    with pytest.raises(HarborChatCapabilityError, match="JSON response mode"):
        client.chat(
            [HarborChatMessage.user("hello")],
            response_format={"type": "json_object"},
        )

    assert invocation.calls == []


def test_multiple_enabled_deployments_are_routed() -> None:
    config = HarborChatClientConfig.from_dict(
        {
            "default_model": "chat",
            "routing": {"strategy": "ordered"},
            "models": {
                "chat": {
                    "deployments": [
                        {
                            "name": "one",
                            "provider": "openai",
                            "model": "openai/gpt-4o-mini",
                            "api_key": "key-one",
                        },
                        {
                            "name": "two",
                            "provider": "anthropic",
                            "model": "anthropic/claude-sonnet-4-5",
                            "api_key": "key-two",
                        },
                    ]
                }
            },
        }
    )
    client = HarborChatClient(config, invocation=FakeInvocation([response_dict()]))

    response = client.chat([HarborChatMessage.user("hello")])

    assert response.deployment == "one"
    assert client._invocation.calls[0]["model"] == "openai/gpt-4o-mini"


def test_request_and_messages_are_mutually_exclusive(base_config) -> None:
    request = HarborChatRequest(messages=(HarborChatMessage.user("request"),))
    client = HarborChatClient(base_config, invocation=FakeInvocation())

    with pytest.raises(HarborChatInvalidRequestError, match="mutually exclusive"):
        client.chat([HarborChatMessage.user("messages")], request=request)


def test_invalid_message_is_reported_as_contract_error(base_config) -> None:
    client = HarborChatClient(base_config, invocation=FakeInvocation())

    with pytest.raises(HarborChatInvalidRequestError, match="invalid chat request"):
        client.chat([{"role": "not-a-role", "content": "hello"}])


def test_custom_litellm_provider_is_supported_when_allowed() -> None:
    config = HarborChatClientConfig.from_dict(
        {
            "default_model": "custom",
            "security": {"allow_custom_providers": True},
            "models": {
                "custom": {
                    "provider": "custom",
                    "model": "vendor/model",
                    "custom_llm_provider": "vendor",
                    "api_key": "key",
                }
            },
        }
    )
    invocation = FakeInvocation([response_dict()])

    HarborChatClient(config, invocation=invocation).chat([HarborChatMessage.user("hello")])

    assert invocation.calls[0]["model"] == "vendor/model"
    assert invocation.calls[0]["custom_llm_provider"] == "vendor"


def test_custom_provider_is_rejected_by_default_security_policy() -> None:
    config = HarborChatClientConfig.from_dict(
        {
            "default_model": "custom",
            "models": {
                "custom": {
                    "provider": "custom",
                    "model": "vendor/model",
                    "custom_llm_provider": "vendor",
                }
            },
        }
    )

    with pytest.raises(HarborChatConfigurationError, match="custom providers"):
        HarborChatClient(config, invocation=FakeInvocation())


def test_cache_and_router_execution_modes_are_accepted(base_config) -> None:
    cached = base_config.model_copy(
        update={"cache": base_config.cache.model_copy(update={"enabled": True})}
    )
    routed = base_config.model_copy(
        update={
            "routing": base_config.routing.model_copy(
                update={"engine": RoutingEngine.LITELLM_ROUTER}
            )
        }
    )

    HarborChatClient(cached, invocation=FakeInvocation())
    HarborChatClient(routed, invocation=FakeInvocation())
