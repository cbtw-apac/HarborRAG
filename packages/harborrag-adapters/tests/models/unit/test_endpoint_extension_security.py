from __future__ import annotations

from typing import Any

import pytest

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.chat.validation import (
    validate_chat_configuration,
    validate_chat_request,
)
from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_adapters.models.embed.validation import (
    validate_embed_configuration,
    validate_embed_request,
)
from harborrag_adapters.models.rerank import HarborRerankClientConfig
from harborrag_adapters.models.rerank.validation import (
    validate_rerank_configuration,
    validate_rerank_request,
)
from harborrag_adapters.models.runtime.provider_validation import (
    is_endpoint_extension_parameter,
)
from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest
from harborrag_core.models.embed import HarborEmbedRequest
from harborrag_core.models.errors import (
    HarborChatConfigurationError,
    HarborChatInvalidRequestError,
    HarborEmbedConfigurationError,
    HarborEmbedInvalidRequestError,
    HarborRerankConfigurationError,
    HarborRerankInvalidRequestError,
)
from harborrag_core.models.rerank import HarborRerankRequest

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]

ALLOWED_HOST = "bedrock-runtime.us-east-1.amazonaws.com"
ALLOWED_ENDPOINT = f"https://{ALLOWED_HOST}"


def _embed(
    extra: dict[str, Any], security: dict[str, Any] | None = None
) -> HarborEmbedClientConfig:
    return HarborEmbedClientConfig.from_dict(
        {
            "default_model": "primary",
            "security": security or {},
            "models": {
                "primary": {
                    "provider": "openai",
                    "model": "openai/embed",
                    "api_key": "key",
                    "extra_litellm_params": extra,
                }
            },
        }
    )


def _rerank(
    extra: dict[str, Any], security: dict[str, Any] | None = None
) -> HarborRerankClientConfig:
    return HarborRerankClientConfig.from_dict(
        {
            "default_model": "primary",
            "security": security or {},
            "models": {
                "primary": {
                    "provider": "cohere",
                    "model": "cohere/rerank",
                    "api_key": "key",
                    "extra_litellm_params": extra,
                }
            },
        }
    )


def _chat(extra: dict[str, Any], security: dict[str, Any] | None = None) -> HarborChatClientConfig:
    return HarborChatClientConfig.from_dict(
        {
            "default_model": "primary",
            "security": security or {},
            "models": {
                "primary": {
                    "provider": "openai",
                    "model": "openai/test",
                    "api_key": "key",
                    "extra_litellm_params": extra,
                }
            },
        }
    )


@pytest.mark.parametrize(
    ("endpoint", "security", "match"),
    [
        ("http://remote.example", {}, "HTTPS"),
        ("https://attacker.example", {"allowed_base_url_hosts": [ALLOWED_HOST]}, "not allowed"),
        ("https://user:pass@" + ALLOWED_HOST, {}, "user information"),
    ],
)
def test_deployment_endpoint_extension_obeys_base_url_policy(
    endpoint: str, security: dict[str, Any], match: str
) -> None:
    extra = {"aws_bedrock_runtime_endpoint": endpoint}
    with pytest.raises(HarborEmbedConfigurationError, match=match):
        validate_embed_configuration(_embed(extra, security))
    with pytest.raises(HarborRerankConfigurationError, match=match):
        validate_rerank_configuration(_rerank(extra, security))
    chat_security = {**security, "allowed_extra_litellm_params": ["aws_bedrock_runtime_endpoint"]}
    with pytest.raises(HarborChatConfigurationError, match=match):
        validate_chat_configuration(_chat(extra, chat_security))


def test_allowed_deployment_endpoint_extension_is_accepted() -> None:
    security = {"allowed_base_url_hosts": [ALLOWED_HOST]}
    extra = {"aws_bedrock_runtime_endpoint": ALLOWED_ENDPOINT}
    validate_embed_configuration(_embed(extra, security))
    validate_rerank_configuration(_rerank(extra, security))


def test_url_valued_extension_is_validated_whatever_its_name() -> None:
    security = {"allowed_extra_litellm_params": ["gateway"]}
    with pytest.raises(HarborEmbedConfigurationError, match="HTTPS"):
        validate_embed_configuration(_embed({"gateway": "http://remote.example"}, security))


def test_non_string_endpoint_extension_is_rejected() -> None:
    with pytest.raises(HarborEmbedConfigurationError, match="URL string"):
        validate_embed_configuration(_embed({"aws_bedrock_runtime_endpoint": 42}))


def test_request_level_endpoint_overrides_are_forbidden() -> None:
    extra = {"aws_bedrock_runtime_endpoint": ALLOWED_ENDPOINT}
    embed_config = _embed({})
    with pytest.raises(HarborEmbedInvalidRequestError, match="endpoint overrides"):
        validate_embed_request(
            HarborEmbedRequest(inputs=("a",), extra_params=extra),
            embed_config,
            embed_config.models["primary"].deployments[0],
        )
    rerank_config = _rerank({})
    with pytest.raises(HarborRerankInvalidRequestError, match="endpoint overrides"):
        validate_rerank_request(
            HarborRerankRequest(
                query="q",
                documents=({"content": "a", "document_id": "a"},),
                extra_params=extra,
            ),
            rerank_config,
            rerank_config.models["primary"].deployments[0],
        )
    chat_config = _chat({}, {"allowed_extra_litellm_params": ["aws_bedrock_runtime_endpoint"]})
    with pytest.raises(HarborChatInvalidRequestError, match="endpoint overrides"):
        validate_chat_request(
            HarborChatRequest(
                messages=(HarborChatMessage.user("hi"),),
                logical_model="primary",
                extra_params=extra,
            ),
            chat_config,
            chat_config.models["primary"].deployments[0],
        )


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("aws_bedrock_runtime_endpoint", "anything", True),
        ("AWS_STS_ENDPOINT", None, True),
        ("custom_url", 1, True),
        ("base_url", "x", True),
        ("gateway", "https://gateway.example", True),
        ("truncate", "END", False),
        ("input_type", "search_query", False),
        ("model_id", "arn:aws:bedrock:us-east-1:1:model/x", False),
    ],
)
def test_endpoint_extension_detection(name: str, value: object, expected: bool) -> None:
    assert is_endpoint_extension_parameter(name, value) is expected
