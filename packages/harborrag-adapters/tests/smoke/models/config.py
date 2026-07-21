from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from harborrag_adapters.models.chat import ChatBackendType, HarborChatClientConfig
from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_adapters.models.rerank import HarborRerankClientConfig


class SmokeConfigurationError(ValueError):
    """Report an invalid live-smoke configuration without exposing its value."""


class SmokeNotConfigured(SmokeConfigurationError):
    """Report that a live-smoke target has no usable real-provider settings."""


def _required(name: str) -> str:
    value = _optional(name)
    if value is None:
        raise SmokeNotConfigured(f"missing required smoke-test environment variable: {name}")
    return value


def _optional(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    if "REPLACE_WITH_REAL" in normalized:
        raise SmokeNotConfigured(f"placeholder smoke-test value is still configured: {name}")
    return normalized


def _boolean(name: str, *, default: bool = False) -> bool:
    value = _optional(name)
    if value is None:
        return default
    normalized = value.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SmokeConfigurationError(f"{name} must be true/false, 1/0, yes/no, or on/off")


def _positive_int(name: str) -> int | None:
    value = _optional(name)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise SmokeConfigurationError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise SmokeConfigurationError(f"{name} must be greater than zero")
    return parsed


def _timeout() -> float:
    value = _optional("HARBOR_SMOKE_TIMEOUT_SECONDS") or "90"
    try:
        timeout = float(value)
    except ValueError as exc:
        raise SmokeConfigurationError("HARBOR_SMOKE_TIMEOUT_SECONDS must be numeric") from exc
    if timeout <= 0:
        raise SmokeConfigurationError("HARBOR_SMOKE_TIMEOUT_SECONDS must be greater than zero")
    return timeout


def _json_mapping(name: str) -> dict[str, Any]:
    value = _optional(name)
    if value is None:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SmokeConfigurationError(f"{name} must contain valid JSON: {exc}") from exc
    if not isinstance(decoded, Mapping):
        raise SmokeConfigurationError(f"{name} must decode to a JSON object")
    return dict(decoded)


def _deployment(prefix: str) -> dict[str, Any]:
    deployment: dict[str, Any] = {
        "name": "smoke",
        "provider": _required(f"{prefix}_PROVIDER"),
        "model": _required(f"{prefix}_MODEL"),
        "allow_ambient_credentials": _boolean(f"{prefix}_ALLOW_AMBIENT_CREDENTIALS", default=False),
    }
    optional_fields = {
        "api_key": "API_KEY",
        "api_base": "API_BASE",
        "api_version": "API_VERSION",
        "deployment_name": "DEPLOYMENT_NAME",
        "custom_llm_provider": "CUSTOM_LLM_PROVIDER",
        "aws_region_name": "AWS_REGION_NAME",
        "aws_access_key_id": "AWS_ACCESS_KEY_ID",
        "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
        "aws_session_token": "AWS_SESSION_TOKEN",
        "aws_role_name": "AWS_ROLE_NAME",
        "aws_role_session_name": "AWS_ROLE_SESSION_NAME",
        "vertex_project": "VERTEX_PROJECT",
        "vertex_location": "VERTEX_LOCATION",
        "vertex_credentials": "VERTEX_CREDENTIALS",
    }
    for field, suffix in optional_fields.items():
        value = _optional(f"{prefix}_{suffix}")
        if value is not None:
            deployment[field] = value
    headers = _json_mapping(f"{prefix}_HEADERS_JSON")
    if headers:
        deployment["headers"] = headers
    extra = _json_mapping(f"{prefix}_EXTRA_LITELLM_PARAMS_JSON")
    if extra:
        deployment["extra_litellm_params"] = extra
    return deployment


def _security(prefix: str) -> dict[str, Any]:
    return {"allow_custom_providers": _boolean(f"{prefix}_ALLOW_CUSTOM_PROVIDER")}


def chat_config() -> HarborChatClientConfig:
    backend_value = _optional("HARBOR_SMOKE_CHAT_BACKEND") or ChatBackendType.DIRECT_SDK.value
    try:
        backend_type = ChatBackendType(backend_value)
    except ValueError as exc:
        supported = ", ".join(item.value for item in ChatBackendType)
        raise SmokeConfigurationError(
            f"HARBOR_SMOKE_CHAT_BACKEND must be one of: {supported}"
        ) from exc

    deployment = _deployment("HARBOR_SMOKE_CHAT")
    deployment["capabilities"] = {"streaming": True}
    backend: dict[str, Any] = {"type": backend_type.value}
    chat: dict[str, Any] = {
        "default_model": "smoke",
        "timeout_seconds": _timeout(),
        "security": _security("HARBOR_SMOKE_CHAT"),
        "retry": _single_attempt_policy(),
        "models": {"smoke": {"deployments": [deployment]}},
        "backend": backend,
    }
    if backend_type is ChatBackendType.LITELLM_ROUTER:
        chat["routing"] = {"engine": "litellm_router", "strategy": "ordered"}
    if backend_type is ChatBackendType.LITELLM_PROXY:
        backend["proxy"] = {
            "api_base": _required("HARBOR_SMOKE_CHAT_PROXY_API_BASE"),
            "api_key": _required("HARBOR_SMOKE_CHAT_PROXY_API_KEY"),
            "headers": _json_mapping("HARBOR_SMOKE_CHAT_PROXY_HEADERS_JSON"),
        }
    return HarborChatClientConfig.from_dict({"chat": chat})


def embed_config() -> HarborEmbedClientConfig:
    deployment = _deployment("HARBOR_SMOKE_EMBED")
    dimensions = _positive_int("HARBOR_SMOKE_EMBED_EXPECTED_DIMENSIONS")
    if dimensions is not None:
        deployment["expected_dimensions"] = dimensions
    deployment["capabilities"] = {
        "batch": True,
        "encoding_format": True,
        "default_dimensions": dimensions,
    }
    return HarborEmbedClientConfig.from_dict(
        {
            "embed": {
                "default_model": "smoke",
                "timeout_seconds": _timeout(),
                "security": _security("HARBOR_SMOKE_EMBED"),
                "retry": _single_attempt_policy(),
                "models": {
                    "smoke": {
                        "embedding_space": _optional("HARBOR_SMOKE_EMBED_SPACE")
                        or "smoke-embedding-space",
                        "deployments": [deployment],
                    }
                },
            }
        }
    )


def rerank_config() -> HarborRerankClientConfig:
    deployment = _deployment("HARBOR_SMOKE_RERANK")
    return HarborRerankClientConfig.from_dict(
        {
            "rerank": {
                "default_model": "smoke",
                "timeout_seconds": _timeout(),
                "security": _security("HARBOR_SMOKE_RERANK"),
                "retry": _single_attempt_policy(),
                "models": {
                    "smoke": {
                        "default_params": {"return_documents": False},
                        "deployments": [deployment],
                    }
                },
            }
        }
    )


def _single_attempt_policy() -> dict[str, int]:
    return {
        "same_deployment_attempts": 1,
        "max_deployment_failovers": 0,
        "max_model_fallbacks": 0,
    }
