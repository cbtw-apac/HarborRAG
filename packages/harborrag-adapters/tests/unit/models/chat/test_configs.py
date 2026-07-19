from __future__ import annotations

import pytest
from harborrag_adapters.models.chat.configs import HarborChatClientConfig
from harborrag_adapters.models.chat.validation import validate_chat_configuration
from harborrag_core.models.errors import HarborChatConfigurationError

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest.mark.parametrize(
    ("name", "entry"),
    [
        ("openai", {"provider": "openai", "model": "openai/gpt-4o", "api_key": "k"}),
        (
            "azure",
            {
                "provider": "azure_openai",
                "model": "azure/gpt-4.1",
                "api_key": "k",
                "api_base": "https://example.openai.azure.com",
                "api_version": "2025-04-01-preview",
                "deployment_name": "gpt-4.1-prod",
            },
        ),
        (
            "bedrock",
            {
                "provider": "bedrock",
                "model": "bedrock/anthropic.claude-sonnet-4-5-v1:0",
                "aws_region_name": "us-east-1",
                "allow_ambient_credentials": True,
            },
        ),
        (
            "anthropic",
            {
                "provider": "anthropic",
                "model": "anthropic/claude-sonnet-4-5",
                "api_key": "k",
            },
        ),
        (
            "gemini",
            {"provider": "gemini", "model": "gemini/gemini-2.5-pro", "api_key": "k"},
        ),
        (
            "ollama",
            {
                "provider": "ollama",
                "model": "ollama/qwen3",
                "api_base": "http://localhost:11434",
            },
        ),
        (
            "compatible",
            {
                "provider": "openai_compatible",
                "model": "openai/local-model",
                "api_base": "https://llm.example.com/v1",
                "api_key": "k",
            },
        ),
    ],
)
def test_provider_configurations(name, entry):
    config = HarborChatClientConfig.from_dict({"default_model": name, "models": {name: entry}})
    assert config.models[name].deployments[0].provider.value == entry["provider"]


def test_azure_validation_rejects_missing_fields():
    config = HarborChatClientConfig.from_dict(
        {
            "default_model": "azure",
            "models": {
                "azure": {
                    "provider": "azure_openai",
                    "model": "azure/gpt-4.1",
                    "api_key": "k",
                }
            },
        }
    )
    with pytest.raises(HarborChatConfigurationError, match="requires"):
        validate_chat_configuration(config)


def test_environment_expansion(monkeypatch):
    monkeypatch.setenv("CHAT_API_KEY", "super-secret")
    config = HarborChatClientConfig.from_dict(
        {
            "default_model": "primary",
            "models": {
                "primary": {
                    "provider": "openai",
                    "model": "openai/gpt-4o",
                    "api_key": "${CHAT_API_KEY}",
                }
            },
        }
    )
    assert config.models["primary"].deployments[0].api_key.get_secret_value() == "super-secret"


def test_profile_and_programmatic_override():
    config = HarborChatClientConfig.from_dict(
        {
            "chat": {
                "default_model": "primary",
                "timeout_seconds": 60,
                "models": {
                    "primary": {
                        "provider": "ollama",
                        "model": "ollama/qwen3",
                        "api_base": "http://localhost:11434",
                    }
                },
            },
            "profiles": {"production": {"chat": {"timeout_seconds": 20}}},
        },
        profile="production",
        overrides={"timeout_seconds": 10},
    )
    assert config.timeout_seconds == 10


def test_secret_is_redacted_from_repr():
    config = HarborChatClientConfig.from_dict(
        {
            "default_model": "primary",
            "models": {
                "primary": {
                    "provider": "openai",
                    "model": "openai/gpt-4o",
                    "api_key": "do-not-print",
                    "headers": {"Authorization": "Bearer do-not-print"},
                }
            },
        }
    )
    assert "do-not-print" not in repr(config)
    assert "**********" in repr(config)


def test_secret_manager_reference_is_resolved():
    class Resolver:
        def resolve(self, reference):
            assert reference.uri == "secret://vault/openai"
            return "resolved-key"

    config = HarborChatClientConfig.from_dict(
        {
            "default_model": "primary",
            "models": {
                "primary": {
                    "provider": "openai",
                    "model": "openai/gpt-4o",
                    "api_key": "secret://vault/openai",
                }
            },
        },
        secret_resolver=Resolver(),
    )
    assert config.models["primary"].deployments[0].api_key.get_secret_value() == "resolved-key"


def test_environment_variable_can_point_to_secret_reference(monkeypatch):
    monkeypatch.setenv("CHAT_SECRET_REF", "secret://vault/chat-key")

    class Resolver:
        def resolve(self, reference):
            assert reference.uri == "secret://vault/chat-key"
            return "resolved-from-env"

    config = HarborChatClientConfig.from_dict(
        {
            "default_model": "primary",
            "models": {
                "primary": {
                    "provider": "openai",
                    "model": "openai/gpt-4o",
                    "api_key": "${CHAT_SECRET_REF}",
                }
            },
        },
        secret_resolver=Resolver(),
    )
    assert config.models["primary"].deployments[0].api_key.get_secret_value() == "resolved-from-env"
