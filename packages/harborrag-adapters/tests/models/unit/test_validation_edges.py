from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

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
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    HarborTokenBudget,
    MessageRole,
)
from harborrag_core.models.embed import (
    EmbeddingEncodingFormat,
    EmbeddingPurpose,
    HarborEmbedRequest,
)
from harborrag_core.models.errors import (
    HarborChatCapabilityError,
    HarborChatConfigurationError,
    HarborChatInvalidRequestError,
    HarborEmbedCapabilityError,
    HarborEmbedConfigurationError,
    HarborEmbedInvalidRequestError,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


class StructuredResult(BaseModel):
    """Provide a response schema for capability validation."""

    answer: str


def _chat_config(
    *,
    capabilities: dict[str, Any] | None = None,
    security: dict[str, Any] | None = None,
) -> HarborChatClientConfig:
    return HarborChatClientConfig.from_dict(
        {
            "default_model": "primary",
            "security": security or {},
            "models": {
                "primary": {
                    "provider": "openai",
                    "model": "openai/test",
                    "api_key": "key",
                    "capabilities": capabilities or {},
                }
            },
        }
    )


def _embed_config(
    *, capabilities: dict[str, Any] | None = None, **updates: Any
) -> HarborEmbedClientConfig:
    document: dict[str, Any] = {
        "default_model": "primary",
        "models": {
            "primary": {
                "provider": "openai",
                "model": "openai/embed",
                "api_key": "key",
                "expected_dimensions": 2,
                "capabilities": capabilities or {},
            }
        },
    }
    document.update(updates)
    return HarborEmbedClientConfig.from_dict(document)


def _chat_request(**updates: Any) -> HarborChatRequest:
    return HarborChatRequest(
        messages=(HarborChatMessage.user("hello"),),
        logical_model="primary",
        **updates,
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"reasoning_effort": "medium"},
        {"token_budget": HarborTokenBudget(max_input_tokens=100)},
    ],
)
def test_chat_rejects_not_yet_supported_baseline_features(
    updates: dict[str, Any],
) -> None:
    config = _chat_config()
    deployment = config.models["primary"].deployments[0]
    with pytest.raises(HarborChatCapabilityError):
        validate_chat_request(_chat_request(**updates), config, deployment)


@pytest.mark.parametrize(
    "updates",
    [
        {"tool_choice": "auto"},
        {"parallel_tool_calls": True},
        {"messages": (HarborChatMessage(role=MessageRole.TOOL, content="result"),)},
    ],
)
def test_chat_rejects_orphaned_tool_controls(updates: dict[str, Any]) -> None:
    config = _chat_config()
    deployment = config.models["primary"].deployments[0]
    base = {"messages": (HarborChatMessage.user("hello"),), "logical_model": "primary"}
    request = HarborChatRequest(**(base | updates))
    with pytest.raises(HarborChatInvalidRequestError):
        validate_chat_request(request, config, deployment)


@pytest.mark.parametrize(
    ("response_format", "capabilities", "error_type"),
    [
        (StructuredResult, {}, HarborChatCapabilityError),
        ({"type": "json_schema", "json_schema": {}}, {}, HarborChatCapabilityError),
        ({"type": "json_object"}, {}, HarborChatCapabilityError),
        ({"type": "text"}, {}, HarborChatInvalidRequestError),
        (StructuredResult, {"structured_output": True}, None),
        ({"type": "json_object"}, {"json_mode": True}, None),
    ],
)
def test_chat_response_format_capabilities(
    response_format: Any,
    capabilities: dict[str, Any],
    error_type: type[Exception] | None,
) -> None:
    config = _chat_config(capabilities=capabilities)
    deployment = config.models["primary"].deployments[0]
    request = _chat_request(response_format=response_format)
    if error_type is None:
        validate_chat_request(request, config, deployment)
    else:
        with pytest.raises(error_type):
            validate_chat_request(request, config, deployment)


@pytest.mark.parametrize(
    ("security", "request_updates", "match"),
    [
        ({"max_extra_params": 1}, {"extra_params": {"a": 1, "b": 2}}, "too many"),
        ({}, {"extra_params": {"unsafe": True}}, "unsafe"),
        ({}, {"custom_headers": {"Authorization": "Bearer secret"}}, "headers"),
    ],
)
def test_chat_request_security_edges(
    security: dict[str, Any], request_updates: dict[str, Any], match: str
) -> None:
    config = _chat_config(security=security)
    deployment = config.models["primary"].deployments[0]
    with pytest.raises(HarborChatInvalidRequestError, match=match):
        validate_chat_request(_chat_request(**request_updates), config, deployment)


def test_chat_configuration_security_edges() -> None:
    disallowed = _chat_config(security={"allowed_providers": ["anthropic"]})
    with pytest.raises(HarborChatConfigurationError, match="not allowed"):
        validate_chat_configuration(disallowed)

    unsafe = _chat_config()
    deployment = (
        unsafe.models["primary"]
        .deployments[0]
        .model_copy(update={"extra_litellm_params": {"unapproved": True}})
    )
    logical = unsafe.models["primary"].model_copy(update={"deployments": (deployment,)})
    with pytest.raises(HarborChatConfigurationError, match="unapproved"):
        validate_chat_configuration(unsafe.model_copy(update={"models": {"primary": logical}}))

    insecure = _chat_config()
    deployment = (
        insecure.models["primary"]
        .deployments[0]
        .model_copy(update={"api_base": "http://remote.example.com"})
    )
    logical = insecure.models["primary"].model_copy(update={"deployments": (deployment,)})
    with pytest.raises(HarborChatConfigurationError, match="HTTPS"):
        validate_chat_configuration(insecure.model_copy(update={"models": {"primary": logical}}))


@pytest.mark.parametrize(
    ("embed_request", "config_updates", "error_type"),
    [
        (
            HarborEmbedRequest(inputs=("a", "b")),
            {"max_inputs_per_request": 1},
            HarborEmbedInvalidRequestError,
        ),
        (
            HarborEmbedRequest(inputs=("long",)),
            {"max_characters_per_input": 2},
            HarborEmbedInvalidRequestError,
        ),
        (
            HarborEmbedRequest(inputs=("a",), extra_params={"a": 1, "b": 2}),
            {"security": {"max_extra_params": 1}},
            HarborEmbedInvalidRequestError,
        ),
        (
            HarborEmbedRequest(inputs=("a",), extra_params={"unsafe": True}),
            {},
            HarborEmbedInvalidRequestError,
        ),
        (
            HarborEmbedRequest(inputs=("a",), custom_headers={"X-Api-Key": "secret"}),
            {},
            HarborEmbedInvalidRequestError,
        ),
        (HarborEmbedRequest(inputs=((1, 2),)), {}, HarborEmbedCapabilityError),
        (
            HarborEmbedRequest(inputs=("a",), dimensions=3),
            {},
            HarborEmbedCapabilityError,
        ),
        (
            HarborEmbedRequest(inputs=("a",), encoding_format=EmbeddingEncodingFormat.BASE64),
            {},
            HarborEmbedCapabilityError,
        ),
        (
            HarborEmbedRequest(inputs=("a",), purpose=EmbeddingPurpose.QUERY),
            {},
            HarborEmbedCapabilityError,
        ),
    ],
)
def test_embedding_request_validation_edges(
    embed_request: HarborEmbedRequest,
    config_updates: dict[str, Any],
    error_type: type[Exception],
) -> None:
    config = _embed_config(**config_updates)
    deployment = config.models["primary"].deployments[0]
    with pytest.raises(error_type):
        validate_embed_request(embed_request, config, deployment)


def test_embedding_semantics_preserving_capability_degradation() -> None:
    config = _embed_config()
    deployment = config.models["primary"].deployments[0]
    request = HarborEmbedRequest(
        inputs=("a",),
        dimensions=2,
        encoding_format=EmbeddingEncodingFormat.FLOAT,
    )
    validated = validate_embed_request(request, config, deployment)
    assert validated.dimensions is None
    assert validated.encoding_format is None


def test_embedding_configuration_rejects_disabled_and_unsafe_routes() -> None:
    config = _embed_config()
    deployment = config.models["primary"].deployments[0].model_copy(update={"enabled": False})
    logical = config.models["primary"].model_copy(update={"deployments": (deployment,)})
    with pytest.raises(HarborEmbedConfigurationError, match="enabled"):
        validate_embed_configuration(config.model_copy(update={"models": {"primary": logical}}))

    deployment = (
        config.models["primary"]
        .deployments[0]
        .model_copy(update={"extra_litellm_params": {"unsafe": True}})
    )
    logical = config.models["primary"].model_copy(update={"deployments": (deployment,)})
    with pytest.raises(HarborEmbedConfigurationError, match="unapproved"):
        validate_embed_configuration(config.model_copy(update={"models": {"primary": logical}}))
