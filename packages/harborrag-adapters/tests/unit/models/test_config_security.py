from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest
from harborrag_adapters.models.chat import (
    HarborChatProviderConfig,
    HarborChatSecurityConfig,
    HarborProvider,
    ProviderRegistry,
)
from harborrag_adapters.models.common.config import (
    BudgetLimitConfig,
    RetryPolicyConfig,
    TimeoutConfig,
)
from harborrag_adapters.models.common.environment import expand_environment
from harborrag_adapters.models.common.loading import (
    load_config_document,
    merge_config_mappings,
    prepare_config_section,
)
from harborrag_adapters.models.common.provider import (
    ImmutableProviderRegistry,
    ProviderDeploymentConfig,
    ProviderMetadata,
)
from harborrag_adapters.models.common.provider_validation import (
    validate_extension_parameters,
    validate_request_headers,
)
from harborrag_adapters.models.common.security import (
    PrivacyConfig,
    PrivacySanitizer,
    SecretReference,
    resolve_secret_references,
    reveal_secret,
    sanitize_configuration,
)
from harborrag_adapters.models.common.transport import (
    protect_sensitive_headers,
    reveal_headers,
    validate_base_url,
)
from model_runtime_support import chat_config
from pydantic import SecretStr, ValidationError


class Resolver:
    """Resolve test secret references predictably."""

    def resolve(self, reference: SecretReference) -> str:
        """Return a value derived from the protected URI."""

        return f"resolved:{reference.uri.rsplit('/', 1)[-1]}"


def test_environment_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_KEY", "abc")
    value = expand_environment(
        {
            "exact": "${MODEL_KEY}",
            "inline": "key=${MODEL_KEY}",
            "default": "${MISSING:-fallback}",
            "items": ["${MODEL_KEY}", ("${MODEL_KEY}",)],
            "number": 7,
        }
    )
    assert value == {
        "exact": "abc",
        "inline": "key=abc",
        "default": "fallback",
        "items": ["abc", ("abc",)],
        "number": 7,
    }
    with pytest.raises(ValueError, match="MISSING"):
        expand_environment("${MISSING}")


@pytest.mark.parametrize("suffix", [".yaml", ".yml", ".json"])
def test_config_document_loading(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"config{suffix}"
    payload = {"chat": {"default_model": "primary"}}
    path.write_text(
        json.dumps(payload) if suffix == ".json" else "chat:\n  default_model: primary\n"
    )
    assert load_config_document(path) == payload


def test_config_document_rejects_invalid_files(tmp_path: Path) -> None:
    text = tmp_path / "config.txt"
    text.write_text("x")
    with pytest.raises(ValueError, match="yaml"):
        load_config_document(text)
    invalid = tmp_path / "config.yaml"
    invalid.write_text("- item")
    with pytest.raises(ValueError, match="mapping"):
        load_config_document(invalid)


def test_profile_and_override_merging() -> None:
    document = {
        "chat": {
            "timeouts": {"request_seconds": 10},
            "routing": {"strategy": "ordered"},
        },
        "profiles": {
            "prod": {"chat": {"timeouts": {"request_seconds": 20}}},
            "embed-only": {"embed": {"default_model": "x"}},
        },
    }
    prepared = prepare_config_section(
        document,
        section="chat",
        profile="prod",
        overrides={"routing": {"strategy": "weighted"}},
    )
    assert prepared["timeouts"]["request_seconds"] == 20
    assert prepared["routing"]["strategy"] == "weighted"
    assert (
        prepare_config_section(document, section="chat", profile="embed-only", overrides=None)[
            "timeouts"
        ]["request_seconds"]
        == 10
    )
    with pytest.raises(ValueError, match="unknown"):
        prepare_config_section(document, section="chat", profile="missing", overrides=None)
    with pytest.raises(ValueError, match="mapping"):
        prepare_config_section({"chat": []}, section="chat", profile=None, overrides=None)


def test_merge_config_mappings_is_recursive_and_non_mutating() -> None:
    base = {"a": {"b": 1}, "items": [1]}
    override = {"a": {"c": 2}, "items": [2]}
    merged = merge_config_mappings(base, override)
    assert merged == {"a": {"b": 1, "c": 2}, "items": [2]}
    assert base == {"a": {"b": 1}, "items": [1]}


def test_secret_reference_and_resolution() -> None:
    reference = SecretReference(uri="secret://vault/key")
    assert "vault/key" not in repr(reference)
    assert str(reference) == "**********"
    unresolved = resolve_secret_references({"key": "secret://vault/key"}, None)
    assert unresolved == {"key": {"uri": "secret://vault/key"}}
    resolved = resolve_secret_references(
        {"a": reference, "b": ["secret://vault/other"]}, Resolver()
    )
    assert resolved["a"].get_secret_value() == "resolved:key"
    assert resolved["b"][0].get_secret_value() == "resolved:other"
    with pytest.raises(ValueError, match="unresolved"):
        reveal_secret(reference)
    assert reveal_secret(SecretStr("plain")) == "plain"
    assert reveal_secret("header") == "header"
    assert reveal_secret(None) is None
    with pytest.raises(ValidationError):
        SecretReference(uri="vault/key")


def test_privacy_sanitizer_redacts_hashes_and_bounds_content() -> None:
    policy = PrivacyConfig(max_logged_content_length=12)
    sanitizer = PrivacySanitizer(policy)
    sanitized = sanitizer.sanitize(
        {
            "api_key": "secret",
            "message": "abcdefghijklmnop",
            "arguments": '{"password":"x","safe":1}',
            "enum": HarborProvider.OPENAI,
            "items": {1, 2},
        }
    )
    assert sanitized["api_key"] == "[REDACTED]"
    assert sanitized["message"] == "abcdefghijkl"
    assert "REDACTED" in sanitized["arguments"]
    assert sanitized["enum"] == "openai"
    metadata = sanitizer.metadata({"user_id": "u", "tenant_id": "t", "ignored": 1})
    assert metadata["user_id"] != "u"
    assert metadata["tenant_id"] != "t"
    assert "ignored" not in metadata
    assert sanitizer.identifier(None) is None
    assert sanitizer.content({"long": "x" * 50}) is not None
    assert PrivacySanitizer(PrivacyConfig(max_logged_content_length=0)).content("x") is None


def test_configuration_sanitization_and_header_protection() -> None:
    deployment = HarborChatProviderConfig(
        name="d",
        provider=HarborProvider.OPENAI,
        model="openai/test",
        api_key="top-secret",
        headers={"Authorization": "Bearer token", "x-safe": "ok"},
        extra_litellm_params={"organization": "org", "password": "hidden"},
    )
    text = repr(deployment)
    assert "top-secret" not in text
    assert "Bearer token" not in text
    assert "hidden" not in text
    dumped = sanitize_configuration(deployment)
    assert dumped["api_key"] == "**********"
    protected = protect_sensitive_headers({"Authorization": "secret", "x": "ok"})
    assert isinstance(protected["Authorization"], SecretStr)
    assert reveal_headers(protected) == {"Authorization": "secret", "x": "ok"}
    assert protect_sensitive_headers("not-a-map") == "not-a-map"


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("ftp://host/api", "invalid"),
        ("https://user:pass@host/api", "user information"),
        ("https://host/api?token=x", "query"),
        ("http://example.com/api", "HTTPS"),
        ("https://other.example/api", "not allowed"),
    ],
)
def test_base_url_restrictions(url: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        validate_base_url(
            url,
            allowed_hosts=frozenset({"allowed.example"}),
            require_https=True,
        )
    validate_base_url(None, allowed_hosts=None, require_https=True)
    validate_base_url("http://localhost:11434", allowed_hosts=None, require_https=True)
    validate_base_url("http://127.0.0.1:8000", allowed_hosts=None, require_https=True)
    validate_base_url(
        "https://allowed.example/v1",
        allowed_hosts=frozenset({"ALLOWED.EXAMPLE"}),
        require_https=True,
    )


def test_header_and_extension_validation() -> None:
    validate_request_headers({"x-safe": "ok"}, allow_auth_headers=False)
    with pytest.raises(ValueError, match="names"):
        validate_request_headers({"bad\nname": "x"}, allow_auth_headers=True)
    with pytest.raises(ValueError, match="values"):
        validate_request_headers({"x": "bad\rvalue"}, allow_auth_headers=True)
    with pytest.raises(ValueError, match="authentication"):
        validate_request_headers({"Authorization": "secret"}, allow_auth_headers=False)
    validate_extension_parameters({"x": 1}, allowed={"x"})
    with pytest.raises(ValueError, match="unapproved"):
        validate_extension_parameters({"x": 1}, allowed=set())
    with pytest.raises(ValueError, match="typed"):
        validate_extension_parameters({"x": 1}, allowed={"x"}, reserved={"x"})


def test_provider_deployment_validators_and_credential_modes() -> None:
    with pytest.raises(ValidationError, match="configured together"):
        ProviderDeploymentConfig(name="d", model="m", aws_access_key_id="id")
    with pytest.raises(ValidationError, match="user information"):
        ProviderDeploymentConfig(name="d", model="m", api_base="https://u:p@host")
    with pytest.raises(ValidationError, match="header values"):
        ProviderDeploymentConfig(name="d", model="m", headers={"x": "a\nb"})
    bedrock = ProviderDeploymentConfig(
        name="d",
        model="bedrock/m",
        aws_region_name="us-east-1",
        aws_access_key_id="id",
        aws_secret_access_key="secret",
    )
    metadata = ProviderMetadata(
        name=HarborProvider.BEDROCK,
        litellm_provider="bedrock",
        required_fields=frozenset({"aws_region_name"}),
        supports_ambient_credentials=True,
        explicit_credential_sets=(frozenset({"aws_access_key_id", "aws_secret_access_key"}),),
    )
    assert bedrock.validate_provider_metadata(metadata) is bedrock
    with pytest.raises(ValueError, match="explicit credentials"):
        ProviderDeploymentConfig(
            name="d", model="bedrock/m", aws_region_name="us-east-1"
        ).validate_provider_metadata(metadata)


def test_registry_is_immutable_and_duplicate_safe() -> None:
    descriptor = ProviderMetadata(name=HarborProvider.OPENAI, litellm_provider="openai")
    registry = ImmutableProviderRegistry([descriptor])
    assert registry.get(HarborProvider.OPENAI) is descriptor
    assert isinstance(registry.all(), MappingProxyType)
    with pytest.raises(TypeError):
        registry.all()[HarborProvider.ANTHROPIC] = descriptor  # type: ignore[index]
    with pytest.raises(ValueError, match="duplicate"):
        ImmutableProviderRegistry([descriptor, descriptor])
    with pytest.raises(KeyError, match="supported"):
        registry.get(HarborProvider.ANTHROPIC)
    assert ProviderRegistry.default().get(HarborProvider.OPENAI).requires_api_key


def test_common_config_validation() -> None:
    assert TimeoutConfig(request_seconds=1).request_seconds == 1
    with pytest.raises(ValidationError, match="max_delay"):
        RetryPolicyConfig(base_delay_seconds=2, max_delay_seconds=1)
    assert BudgetLimitConfig(max_budget=1, budget_duration="1h").max_budget == 1
    with pytest.raises(ValidationError, match="configured together"):
        BudgetLimitConfig(max_budget=1)
    config = chat_config()
    assert config.sanitized_dump()["models"]["primary"]["deployments"][0]["api_key"] == "**********"
    assert HarborChatSecurityConfig().allow_custom_providers is False
