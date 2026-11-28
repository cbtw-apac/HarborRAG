from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.chat.configs import HarborChatProviderConfig
from harborrag_adapters.models.chat.parameters import build_litellm_parameters
from harborrag_adapters.models.chat.registry import ProviderRegistry
from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest
from harborrag_core.models.errors import HarborChatInvalidRequestError

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _litellm_parameters(
    deployment: HarborChatProviderConfig, request: HarborChatRequest, **options: Any
) -> dict[str, Any]:
    metadata = ProviderRegistry.default().get(deployment.provider)
    return build_litellm_parameters(deployment, request, metadata=metadata, **options)


def test_azure_parameters_include_provider_transport_fields() -> None:
    config = HarborChatClientConfig.from_dict(
        {
            "default_model": "azure",
            "models": {
                "azure": {
                    "provider": "azure_openai",
                    "model": "gpt-4.1",
                    "deployment_name": "gpt-4.1-prod",
                    "api_key": "key",
                    "api_base": "https://example.openai.azure.com",
                    "api_version": "2025-04-01-preview",
                }
            },
        }
    )
    request = HarborChatRequest(messages=(HarborChatMessage.user("hello"),), temperature=0.2)

    parameters = _litellm_parameters(config.models["azure"].deployments[0], request, timeout=30)

    assert parameters["model"] == "azure/gpt-4.1-prod"
    assert parameters["api_base"] == "https://example.openai.azure.com"
    assert parameters["api_version"] == "2025-04-01-preview"
    assert parameters["api_key"] == "key"
    assert parameters["messages"] == [{"role": "user", "content": "hello"}]
    assert parameters["temperature"] == 0.2
    assert parameters["timeout"] == 30


def test_request_headers_override_deployment_headers() -> None:
    config = HarborChatClientConfig.from_dict(
        {
            "default_model": "gateway",
            "models": {
                "gateway": {
                    "provider": "openai_compatible",
                    "model": "openai/test",
                    "api_base": "https://gateway.example.com/v1",
                    "headers": {"X-Region": "east", "X-Source": "deployment"},
                }
            },
        }
    )
    request = HarborChatRequest(
        messages=(HarborChatMessage.user("hello"),),
        custom_headers={"X-Tenant": "tenant-a", "X-Source": "request"},
    )

    parameters = _litellm_parameters(config.models["gateway"].deployments[0], request, timeout=30)

    assert parameters["extra_headers"] == {
        "X-Region": "east",
        "X-Source": "request",
        "X-Tenant": "tenant-a",
    }


def test_extra_params_cannot_replace_normalized_fields(base_config) -> None:
    request = HarborChatRequest(
        messages=(HarborChatMessage.user("hello"),),
        extra_params={"timeout": 1},
    )

    with pytest.raises(HarborChatInvalidRequestError, match="timeout"):
        _litellm_parameters(base_config.models["primary"].deployments[0], request, timeout=30)
