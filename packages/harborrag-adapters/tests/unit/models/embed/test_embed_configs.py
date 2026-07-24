from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_adapters.models.common.security import SecretReference
from harborrag_adapters.models.embed import (
    HarborEmbedClientConfig,
    HarborEmbedProvider,
)
from harborrag_adapters.models.embed.validation import validate_embed_configuration
from harborrag_core.models.errors import HarborEmbedConfigurationError

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _document(provider: str, **deployment):
    return {
        "embed": {
            "default_model": "primary",
            "models": {
                "primary": {
                    "provider": provider,
                    "model": deployment.pop("model", "provider/embed-model"),
                    **deployment,
                }
            },
        }
    }


def test_openai_config_and_secret_repr() -> None:
    config = HarborEmbedClientConfig.from_dict(
        _document("openai", api_key="super-secret", expected_dimensions=1536)
    )
    deployment = config.models["primary"].deployments[0]
    assert deployment.provider is HarborEmbedProvider.OPENAI
    assert deployment.expected_dimensions == 1536
    assert "super-secret" not in repr(config)


def test_azure_openai_requires_endpoint_version_and_deployment() -> None:
    incomplete = HarborEmbedClientConfig.from_dict(_document("azure_openai", api_key="key"))
    with pytest.raises(HarborEmbedConfigurationError, match="requires"):
        validate_embed_configuration(incomplete)

    config = HarborEmbedClientConfig.from_dict(
        _document(
            "azure_openai",
            model="azure/text-embedding-3-large",
            api_key="key",
            api_base="https://example.openai.azure.com",
            api_version="2025-04-01-preview",
            deployment_name="embed-production",
        )
    )
    assert config.models["primary"].deployments[0].deployment_name == "embed-production"


def test_bedrock_supports_ambient_credentials() -> None:
    config = HarborEmbedClientConfig.from_dict(
        _document(
            "bedrock",
            model="bedrock/amazon.titan-embed-text-v2:0",
            aws_region_name="us-east-1",
            allow_ambient_credentials=True,
        )
    )
    assert config.models["primary"].deployments[0].aws_region_name == "us-east-1"


@pytest.mark.parametrize(
    ("provider", "extra"),
    [
        ("cohere", {"api_key": "key", "model": "cohere/embed-v4.0"}),
        ("gemini", {"api_key": "key", "model": "gemini/gemini-embedding-001"}),
        (
            "ollama",
            {"api_base": "http://localhost:11434", "model": "ollama/nomic-embed-text"},
        ),
        (
            "openai_compatible",
            {
                "api_base": "https://models.example.com/v1",
                "model": "openai/custom-embed",
            },
        ),
    ],
)
def test_representative_provider_configs(provider: str, extra: dict[str, str]) -> None:
    config = HarborEmbedClientConfig.from_dict(_document(provider, **extra))
    assert config.models["primary"].deployments[0].provider.value == provider


def test_environment_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBOR_EMBED_KEY", "env-secret")
    config = HarborEmbedClientConfig.from_dict(_document("openai", api_key="${HARBOR_EMBED_KEY}"))
    assert config.models["primary"].deployments[0].api_key.get_secret_value() == "env-secret"


def test_secret_reference_and_resolver() -> None:
    class Resolver:
        def resolve(self, reference: SecretReference) -> str:
            assert reference.uri == "secret://vault/embed/key"
            return "resolved-secret"

    unresolved = HarborEmbedClientConfig.from_dict(
        _document("openai", api_key="secret://vault/embed/key")
    )
    assert isinstance(unresolved.models["primary"].deployments[0].api_key, SecretReference)

    resolved = HarborEmbedClientConfig.from_dict(
        _document("openai", api_key="secret://vault/embed/key"),
        secret_resolver=Resolver(),
    )
    assert resolved.models["primary"].deployments[0].api_key.get_secret_value() == "resolved-secret"


def test_profiles_and_programmatic_overrides() -> None:
    document = _document("openai", api_key="key")
    document["profiles"] = {"production": {"embed": {"timeout_seconds": 45}}}
    config = HarborEmbedClientConfig.from_dict(
        document,
        profile="production",
        overrides={"default_batch_size": 32},
    )
    assert config.timeout_seconds == 45
    assert config.default_batch_size == 32


def test_mixed_dimensions_in_one_logical_model_are_rejected() -> None:
    with pytest.raises(ValidationError, match="same dimensions"):
        HarborEmbedClientConfig.from_dict(
            {
                "embed": {
                    "default_model": "primary",
                    "models": {
                        "primary": {
                            "deployments": [
                                {
                                    "name": "a",
                                    "provider": "openai",
                                    "model": "openai/a",
                                    "api_key": "key",
                                    "expected_dimensions": 2,
                                },
                                {
                                    "name": "b",
                                    "provider": "openai",
                                    "model": "openai/b",
                                    "api_key": "key",
                                    "expected_dimensions": 3,
                                },
                            ]
                        }
                    },
                }
            }
        )


def test_missing_environment_variable_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
    with pytest.raises(ValueError, match="DOES_NOT_EXIST"):
        HarborEmbedClientConfig.from_dict(_document("openai", api_key="${DOES_NOT_EXIST}"))


def test_cross_space_logical_fallback_is_rejected() -> None:
    with pytest.raises(ValidationError, match="incompatible embedding spaces"):
        HarborEmbedClientConfig.from_dict(
            {
                "embed": {
                    "default_model": "primary",
                    "models": {
                        "primary": {
                            "fallbacks": ["backup"],
                            "provider": "openai",
                            "model": "openai/a",
                            "api_key": "a",
                            "expected_dimensions": 2,
                        },
                        "backup": {
                            "provider": "cohere",
                            "model": "cohere/b",
                            "api_key": "b",
                            "expected_dimensions": 2,
                        },
                    },
                }
            }
        )
