from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from model_invocation_support import FakeEmbeddingInvocation
from pydantic import SecretStr, ValidationError

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.chat.validation import validate_chat_configuration
from harborrag_adapters.models.embed import (
    HarborEmbedClient,
    HarborEmbedClientConfig,
    HarborEmbedProvider,
    HarborEmbedProviderConfig,
)
from harborrag_adapters.models.runtime.loading import load_config_document
from harborrag_adapters.models.runtime.provider_validation import validate_request_headers
from harborrag_core.models.errors import HarborChatConfigurationError, HarborEmbedConfigurationError

pytestmark = pytest.mark.unit

_REPOSITORY = Path(__file__).resolve().parents[5]
_AMBIENT_KEY = "sk-ambient-tail-4e1b"


def _gateway_embed(**overrides: object) -> HarborEmbedProviderConfig:
    values: dict[str, object] = {
        "name": "gateway-embed",
        "provider": HarborEmbedProvider.OPENAI_COMPATIBLE,
        "model": "text-embedding-test",
        "api_base": "https://gateway.example.com/v1",
        "api_key": "synthetic-key",
        **overrides,
    }
    return HarborEmbedProviderConfig.model_validate(values)


def _embed_catalog(deployment: dict[str, object]) -> HarborEmbedClientConfig:
    return HarborEmbedClientConfig.from_dict(
        {
            "embed": {
                "default_model": "primary",
                "models": {"primary": {"deployments": [deployment]}},
            }
        }
    )


@pytest.mark.parametrize("blank", ["", "   ", "\r\n"])
def test_shipped_catalog_refuses_a_blank_embed_key_instead_of_the_ambient_one(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    monkeypatch.setenv("HARBORRAG_CHAT_MODEL", "chat-test")
    monkeypatch.setenv("HARBORRAG_CHAT_API_KEY", "synthetic-chat-key")
    monkeypatch.setenv("HARBORRAG_EMBED_MODEL", "text-embedding-test")
    monkeypatch.setenv("HARBORRAG_EMBED_API_KEY", blank)
    monkeypatch.setenv("HARBORRAG_URL", "https://gateway.example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", _AMBIENT_KEY)
    monkeypatch.delenv("HARBORRAG_EMBED_PROVIDER", raising=False)
    config = HarborEmbedClientConfig.from_file(_REPOSITORY / "config/models/models.yaml")
    invocation = FakeEmbeddingInvocation([])

    with pytest.raises(HarborEmbedConfigurationError, match="empty api_key") as caught:
        HarborEmbedClient(config, invocation=invocation)

    assert "openai-compatible-embedding" in str(caught.value)
    assert _AMBIENT_KEY not in str(caught.value)
    assert invocation.calls == []


def test_blank_chat_key_is_refused_for_a_provider_that_needs_no_key() -> None:
    config = HarborChatClientConfig.from_dict(
        {
            "chat": {
                "default_model": "primary",
                "models": {
                    "primary": {
                        "deployments": [
                            {
                                "name": "gateway-chat",
                                "provider": "openai_compatible",
                                "model": "chat-test",
                                "api_base": "https://gateway.example.com/v1",
                                "api_key": "",
                            }
                        ]
                    }
                },
            }
        }
    )

    with pytest.raises(HarborChatConfigurationError, match="gateway-chat.*empty api_key"):
        validate_chat_configuration(config)


def test_expanded_values_lose_surrounding_whitespace_and_crlf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_KEY", " synthetic-key\r")
    monkeypatch.setenv("REVIEW_MODEL", "text-embedding-test\r")
    monkeypatch.setenv("REVIEW_BASE", "https://gateway.example.com/v1\r\n")
    monkeypatch.setenv("REVIEW_DEPLOYMENT", "\tembed-deployment ")

    config = _embed_catalog(
        {
            "name": "azure-embed",
            "provider": "azure_openai",
            "model": "${REVIEW_MODEL}",
            "api_base": "${REVIEW_BASE}",
            "api_key": "${REVIEW_KEY}",
            "api_version": "2024-10-21",
            "deployment_name": "${REVIEW_DEPLOYMENT}",
        }
    )

    deployment = config.models["primary"].deployments[0]
    assert isinstance(deployment.api_key, SecretStr)
    assert deployment.api_key.get_secret_value() == "synthetic-key"
    assert deployment.model == "text-embedding-test"
    assert deployment.api_base == "https://gateway.example.com/v1"
    assert deployment.deployment_name == "embed-deployment"


@pytest.mark.parametrize("field", ["api_key", "model", "api_base", "deployment_name"])
def test_embedded_line_break_is_refused_without_echoing_the_value(field: str) -> None:
    injected = "synthetic-tail-77aa\r\nX-Injected: yes"

    with pytest.raises(ValidationError) as caught:
        _gateway_embed(**{field: injected})

    message = str(caught.value)
    assert f"{field} must not contain line breaks" in message
    assert "77aa" not in message
    assert "input_value" not in message


@pytest.mark.parametrize(
    "header",
    [
        "Ocp-Apim-Subscription-Key",
        "X-Goog-Api-Key",
        "X-Auth-Token",
        "X-Client-Secret",
        "X-Request-Signature",
        "Cookie",
        "Authorization",
    ],
)
def test_credential_like_headers_are_masked(header: str) -> None:
    config = _embed_catalog(
        {
            "name": "gateway-embed",
            "provider": "openai_compatible",
            "model": "text-embedding-test",
            "api_base": "https://gateway.example.com/v1",
            "api_key": "synthetic-key",
            "headers": {header: "header-value-5d2c", "X-Request-Source": "harbor"},
        }
    )
    headers = config.models["primary"].deployments[0].headers

    assert isinstance(headers[header], SecretStr)
    assert headers["X-Request-Source"] == "harbor"
    assert "5d2c" not in repr(config)
    assert "5d2c" not in json.dumps(config.sanitized_dump(), default=str)


@pytest.mark.parametrize("header", ["Ocp-Apim-Subscription-Key", "X-Goog-Api-Key", "X-Auth-Token"])
def test_request_credential_headers_follow_the_auth_header_policy(header: str) -> None:
    with pytest.raises(ValueError, match="request-level authentication headers are disabled"):
        validate_request_headers({header: "value"}, allow_auth_headers=False)

    validate_request_headers({header: "value"}, allow_auth_headers=True)
    validate_request_headers({"X-Request-Source": "harbor"}, allow_auth_headers=False)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_catalog_with_a_duplicate_key_is_refused(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "models.yaml",
        "embed:\n  timeouts:\n    request_seconds: 5\n  timeouts:\n    request_seconds: 60\n",
    )

    with pytest.raises(yaml.YAMLError, match="duplicate key 'timeouts'"):
        load_config_document(path)


def test_json_catalog_with_a_duplicate_key_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, "models.json", '{"embed": {}, "embed": {}}')

    with pytest.raises(ValueError, match="duplicate key 'embed'"):
        load_config_document(path)


def test_catalog_with_an_unknown_section_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, "models.yaml", "embed: {}\nreranker: {}\n")

    with pytest.raises(ValueError, match="unknown top-level key.*reranker"):
        load_config_document(path)


def test_single_family_document_still_loads(tmp_path: Path) -> None:
    path = _write(tmp_path, "embed.yaml", "default_model: primary\nmodels: {}\n")

    assert load_config_document(path) == {"default_model": "primary", "models": {}}


@pytest.mark.parametrize(
    "catalog",
    [
        "config/models/models.yaml",
        "config/models/models.production.example.yaml",
        "packages/harborrag-runtime/tests/unit/fixtures/chat_models.yaml",
    ],
)
def test_repository_catalogs_pass_the_strict_loader(catalog: str) -> None:
    assert load_config_document(_REPOSITORY / catalog)
