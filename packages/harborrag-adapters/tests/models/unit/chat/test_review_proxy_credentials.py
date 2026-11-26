"""The chat proxy refuses credential-like request headers and an empty proxy key."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_adapters.models.chat.backend_config import LiteLLMProxyConfig, ProxyAuthMode
from harborrag_adapters.models.chat.backends.proxy import LiteLLMProxyBackend
from harborrag_adapters.models.runtime.config import ConnectionPoolConfig
from harborrag_adapters.models.runtime.connections import SharedConnectionLifecycle

pytestmark = pytest.mark.unit


def _backend(config: LiteLLMProxyConfig) -> LiteLLMProxyBackend:
    return LiteLLMProxyBackend(
        config,
        connections=SharedConnectionLifecycle(ConnectionPoolConfig(enabled=False)),
        completion=lambda **kwargs: kwargs,
        acompletion=lambda **kwargs: kwargs,
    )


@pytest.mark.parametrize("header", ["X-Goog-Api-Key", "X-Auth-Token", "Cookie"])
def test_proxy_refuses_a_credential_like_request_header(header: str) -> None:
    backend = _backend(LiteLLMProxyConfig(api_base="https://proxy.example.test", api_key="k"))

    with pytest.raises(ValueError, match="cannot override"):
        backend.prepare_parameters({"model": "alias", "extra_headers": {header: "attacker"}})


def test_proxy_keeps_a_configured_gateway_credential_beside_its_own_key() -> None:
    backend = _backend(
        LiteLLMProxyConfig(
            api_base="https://proxy.example.test",
            api_key="proxy-secret",
            auth_mode=ProxyAuthMode.X_LITELLM_API_KEY,
            headers={"Authorization": "stale-bearer", "CF-Access-Client-Secret": "gateway"},
        )
    )

    result = backend.prepare_parameters({"model": "alias"})

    assert result["extra_headers"] == {
        "CF-Access-Client-Secret": "gateway",
        "x-litellm-api-key": "proxy-secret",
    }


def test_proxy_key_loses_the_padding_of_a_crlf_env_file() -> None:
    config = LiteLLMProxyConfig(api_base="https://proxy.example.test", api_key=" proxy-secret\r")

    assert _backend(config).prepare_parameters({"model": "alias"})["api_key"] == "proxy-secret"


@pytest.mark.parametrize("blank", ["", "   ", "\r\n"])
def test_proxy_refuses_an_empty_key(blank: str) -> None:
    with pytest.raises(ValidationError, match="api_key is empty"):
        LiteLLMProxyConfig(api_base="https://proxy.example.test", api_key=blank)


def test_proxy_key_error_does_not_echo_the_key() -> None:
    with pytest.raises(ValidationError) as caught:
        LiteLLMProxyConfig(api_base="https://proxy.example.test", api_key="proxy\nsecret-9a2f")

    assert "secret-9a2f" not in str(caught.value)
