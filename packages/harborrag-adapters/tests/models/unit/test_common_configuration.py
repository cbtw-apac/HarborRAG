from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

import pytest
from pydantic import ValidationError

from harborrag_adapters.models.chat.configs import (
    HarborChatClientConfig,
    HarborChatModelConfig,
    HarborChatProviderConfig,
)
from harborrag_adapters.models.chat.validation import validate_chat_configuration
from harborrag_adapters.models.common.config import RetryPolicyConfig
from harborrag_adapters.models.common.context import update_operation_context
from harborrag_adapters.models.common.environment import expand_environment
from harborrag_adapters.models.common.lifecycle import (
    AsyncLifecycleResource,
    ResourceOwnership,
    close_async_resources,
)
from harborrag_adapters.models.common.provider import (
    ImmutableProviderRegistry,
    ProviderMetadata,
)
from harborrag_adapters.models.common.security import SecretReference
from harborrag_core.models.context import ModelOperationContext
from harborrag_core.models.errors import HarborChatConfigurationError

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def chat_document(**deployment: object) -> dict[str, object]:
    return {
        "chat": {
            "default_model": "primary",
            "models": {
                "primary": {
                    "provider": "openai",
                    "model": "openai/gpt-4o",
                    "api_key": "key",
                    **deployment,
                }
            },
        }
    }


def test_environment_expands_complete_embedded_and_default_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARBOR_HOST", "models.example.com")
    monkeypatch.setenv("HARBOR_KEY", "secret-value")

    expanded = expand_environment(
        {
            "api_key": "${HARBOR_KEY}",
            "api_base": "https://${HARBOR_HOST}/v1",
            "region": "${HARBOR_REGION:-us-east-1}",
        }
    )

    assert expanded == {
        "api_key": "secret-value",
        "api_base": "https://models.example.com/v1",
        "region": "us-east-1",
    }


def test_missing_environment_reference_reports_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HARBOR_MISSING_KEY", raising=False)

    with pytest.raises(ValueError, match="HARBOR_MISSING_KEY"):
        HarborChatClientConfig.from_dict(chat_document(api_key="${HARBOR_MISSING_KEY}"))


def test_secret_reference_repr_and_sanitized_output_never_expose_secrets() -> None:
    config = HarborChatClientConfig.from_dict(
        chat_document(
            api_key="secret://vault/chat/key",
            headers={"Authorization": "Bearer header-secret"},
            extra_litellm_params={"access_token": "extension-secret"},
        )
    )

    deployment = config.models["primary"].deployments[0]
    assert isinstance(deployment.api_key, SecretReference)
    rendered = repr(config)
    sanitized = json.dumps(config.sanitized_dump())
    for secret in (
        "secret://vault/chat/key",
        "Bearer header-secret",
        "extension-secret",
    ):
        assert secret not in rendered
        assert secret not in sanitized


def test_python_configuration_objects_and_logical_aliases() -> None:
    deployment = HarborChatProviderConfig(
        name="primary-openai",
        provider="openai",
        model="openai/gpt-4o",
        api_key="key",
    )
    config = HarborChatClientConfig(
        default_model="assistant",
        models={
            "primary": HarborChatModelConfig(
                deployments=(deployment,), aliases=frozenset({"assistant"})
            )
        },
    )

    assert config.model_for()[0] == "primary"
    assert config.timeout_seconds == 60


def test_typed_timeout_configuration_preserves_runtime_accessors() -> None:
    document = chat_document()
    chat = document["chat"]
    assert isinstance(chat, dict)
    chat["timeouts"] = {"request_seconds": 12, "stream_seconds": 30}

    config = HarborChatClientConfig.from_dict(document)

    assert config.timeouts.request_seconds == 12
    assert config.timeout_seconds == 12
    assert config.stream_timeout_seconds == 30


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
def test_configuration_loading_from_json_and_yaml(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"models{suffix}"
    document = chat_document()
    if suffix == ".json":
        path.write_text(json.dumps(document), encoding="utf-8")
    else:
        path.write_text(
            """chat:
  default_model: primary
  models:
    primary:
      provider: openai
      model: openai/gpt-4o
      api_key: key
""",
            encoding="utf-8",
        )

    assert HarborChatClientConfig.from_file(path).default_model == "primary"


def test_unknown_and_incomplete_providers_are_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown-provider"):
        HarborChatClientConfig.from_dict(chat_document(provider="unknown-provider"))
    incomplete = HarborChatClientConfig.from_dict(
        chat_document(
            provider="azure_openai",
            api_base="https://example.openai.azure.com",
            deployment_name="production",
        )
    )
    with pytest.raises(HarborChatConfigurationError, match="api_version"):
        validate_chat_configuration(incomplete)
    ambient = HarborChatClientConfig.from_dict(
        chat_document(api_key=None, allow_ambient_credentials=True)
    )
    with pytest.raises(HarborChatConfigurationError, match="does not support ambient credentials"):
        validate_chat_configuration(ambient)


class ExampleProvider(StrEnum):
    FIRST = "first"
    SECOND = "second"


def test_registry_rejects_duplicates_and_reports_supported_providers() -> None:
    descriptor = ProviderMetadata(ExampleProvider.FIRST, "first")

    with pytest.raises(ValueError, match="duplicate provider registration"):
        ImmutableProviderRegistry([descriptor, descriptor])
    registry = ImmutableProviderRegistry([descriptor])
    with pytest.raises(KeyError, match="supported providers: first"):
        registry.get(ExampleProvider.SECOND)


def test_invalid_timeout_retry_and_credential_combinations_are_rejected() -> None:
    with pytest.raises(ValidationError, match="legacy fields"):
        HarborChatClientConfig.from_dict(
            {
                **chat_document(),
                "chat": {
                    **chat_document()["chat"],
                    "timeouts": {"request_seconds": 10},
                    "timeout_seconds": 5,
                },
            }
        )
    with pytest.raises(ValidationError, match="max_delay_seconds"):
        RetryPolicyConfig(base_delay_seconds=2, max_delay_seconds=1)
    with pytest.raises(ValidationError, match="configured together"):
        HarborChatClientConfig.from_dict(chat_document(aws_access_key_id="access-only"))


@pytest.mark.asyncio
async def test_lifecycle_closes_owned_resources_and_leaves_borrowed_resources_open() -> None:
    events: list[str] = []

    async def owned() -> None:
        events.append("owned")

    async def borrowed() -> None:
        events.append("borrowed")

    await close_async_resources(
        [
            AsyncLifecycleResource(borrowed, ResourceOwnership.BORROWED),
            AsyncLifecycleResource(owned),
        ]
    )

    assert events == ["owned"]


def test_operation_context_adapter_normalizes_provider_identifiers() -> None:
    context = ModelOperationContext(request_id="request", logical_model="primary")

    update_operation_context(
        context,
        provider=ExampleProvider.FIRST,
        provider_model="provider/model",
        deployment="deployment-a",
    )

    assert (context.provider, context.provider_model, context.deployment) == (
        "first",
        "provider/model",
        "deployment-a",
    )
