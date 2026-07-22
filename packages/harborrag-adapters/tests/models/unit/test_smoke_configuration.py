from __future__ import annotations

import pytest
from models.smoke import config


def _set_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    family: str,
    provider: str,
    model: str,
) -> None:
    prefix = {
        "CHAT": "HARBOR_CHAT",
        "EMBED": "HARBOR_EMBED",
        "RERANK": "HARBOR_SMOKE_RERANK",
    }[family]
    monkeypatch.setenv(f"{prefix}_PROVIDER", provider)
    monkeypatch.setenv(f"{prefix}_MODEL", model)
    monkeypatch.setenv(f"{prefix}_API_KEY", "test-key")


def test_smoke_config_builders_accept_explicit_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_provider(monkeypatch, family="CHAT", provider="openai", model="openai/chat")
    _set_provider(monkeypatch, family="EMBED", provider="openai", model="openai/embed")
    _set_provider(
        monkeypatch, family="RERANK", provider="cohere", model="cohere/rerank"
    )
    monkeypatch.setenv("HARBOR_EMBED_EXPECTED_DIMENSIONS", "8")

    chat = config.chat_config()
    embed = config.embed_config()
    rerank = config.rerank_config()

    assert chat.models["smoke"].deployments[0].provider.value == "openai"
    assert embed.models["smoke"].deployments[0].expected_dimensions == 8
    assert rerank.models["smoke"].default_params.return_documents is False


def test_smoke_config_supports_ambient_cloud_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = "HARBOR_EMBED"
    monkeypatch.setenv(f"{prefix}_PROVIDER", "vertex_ai")
    monkeypatch.setenv(f"{prefix}_MODEL", "vertex_ai/embedding-model")
    monkeypatch.setenv(f"{prefix}_ALLOW_AMBIENT_CREDENTIALS", "true")
    monkeypatch.setenv(f"{prefix}_VERTEX_PROJECT", "project")
    monkeypatch.setenv(f"{prefix}_VERTEX_LOCATION", "us-central1")

    smoke_config = config.embed_config()

    assert smoke_config.models["smoke"].deployments[0].allow_ambient_credentials is True


def test_smoke_config_rejects_missing_or_placeholder_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HARBOR_CHAT_PROVIDER", raising=False)
    with pytest.raises(config.SmokeNotConfigured, match="missing required"):
        config.chat_config()

    monkeypatch.setenv("HARBOR_CHAT_PROVIDER", "openai")
    monkeypatch.setenv("HARBOR_CHAT_MODEL", "REPLACE_WITH_REAL_CHAT_MODEL")
    with pytest.raises(config.SmokeNotConfigured, match="placeholder"):
        config.chat_config()


def test_smoke_config_rejects_invalid_typed_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_provider(monkeypatch, family="CHAT", provider="openai", model="openai/chat")
    monkeypatch.setenv("HARBOR_CHAT_ALLOW_AMBIENT_CREDENTIALS", "sometimes")
    with pytest.raises(config.SmokeConfigurationError, match="must be true/false"):
        config.chat_config()

    monkeypatch.setenv("HARBOR_CHAT_ALLOW_AMBIENT_CREDENTIALS", "false")
    monkeypatch.setenv("HARBOR_SMOKE_TIMEOUT_SECONDS", "zero")
    with pytest.raises(config.SmokeConfigurationError, match="must be numeric"):
        config.chat_config()


def test_smoke_chat_config_supports_router_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_provider(monkeypatch, family="CHAT", provider="openai", model="openai/chat")
    monkeypatch.setenv("HARBOR_CHAT_BACKEND", "litellm_router")

    smoke_config = config.chat_config()

    assert smoke_config.backend.type.value == "litellm_router"
    assert smoke_config.routing.engine.value == "litellm_router"


def test_smoke_chat_config_supports_proxy_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_provider(
        monkeypatch,
        family="CHAT",
        provider="litellm_proxy",
        model="gateway-chat",
    )
    monkeypatch.setenv("HARBOR_CHAT_BACKEND", "litellm_proxy")
    monkeypatch.setenv("HARBOR_CHAT_PROXY_API_BASE", "https://proxy.example.test")
    monkeypatch.setenv("HARBOR_CHAT_PROXY_API_KEY", "proxy-key")

    smoke_config = config.chat_config()

    assert smoke_config.backend.proxy is not None
    assert smoke_config.backend.proxy.api_base == "https://proxy.example.test"
