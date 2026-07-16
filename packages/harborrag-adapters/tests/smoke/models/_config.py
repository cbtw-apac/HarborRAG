from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

import pytest
from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_adapters.models.rerank import HarborRerankClientConfig


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        pytest.skip(f"missing required smoke-test environment variable: {name}")
    normalized = value.strip()
    if "REPLACE_WITH_REAL" in normalized:
        pytest.skip(f"placeholder smoke-test environment variable is not configured: {name}")
    return normalized


def _optional(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    if "REPLACE_WITH_REAL" in normalized:
        pytest.fail(
            f"replace the placeholder value for {name} in the smoke-test dotenv file",
            pytrace=False,
        )
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
    pytest.fail(f"{name} must be one of true/false, 1/0, yes/no, or on/off", pytrace=False)


def _positive_int(name: str) -> int | None:
    value = _optional(name)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        pytest.fail(f"{name} must be an integer", pytrace=False)
    if parsed <= 0:
        pytest.fail(f"{name} must be greater than zero", pytrace=False)
    return parsed


def _timeout() -> float:
    value = _optional("HARBOR_SMOKE_TIMEOUT_SECONDS") or "90"
    try:
        parsed = float(value)
    except ValueError:
        pytest.fail("HARBOR_SMOKE_TIMEOUT_SECONDS must be numeric", pytrace=False)
    if parsed <= 0:
        pytest.fail("HARBOR_SMOKE_TIMEOUT_SECONDS must be greater than zero", pytrace=False)
    return parsed


def _json_mapping(name: str) -> dict[str, Any]:
    value = _optional(name)
    if value is None:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        pytest.fail(f"{name} must contain valid JSON: {exc}", pytrace=False)
    if not isinstance(decoded, Mapping):
        pytest.fail(f"{name} must decode to a JSON object", pytrace=False)
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


def chat_config() -> HarborChatClientConfig:
    return HarborChatClientConfig.from_dict(
        {
            "chat": {
                "default_model": "smoke",
                "timeout_seconds": _timeout(),
                "retry": {
                    "same_deployment_attempts": 1,
                    "max_deployment_failovers": 0,
                    "max_model_fallbacks": 0,
                },
                "models": {"smoke": {"deployments": [_deployment("HARBOR_SMOKE_CHAT")]}},
            }
        }
    )


def embed_config() -> HarborEmbedClientConfig:
    deployment = _deployment("HARBOR_SMOKE_EMBED")
    dimensions = _positive_int("HARBOR_SMOKE_EMBED_EXPECTED_DIMENSIONS")
    if dimensions is not None:
        deployment["expected_dimensions"] = dimensions
    embedding_space = _optional("HARBOR_SMOKE_EMBED_SPACE") or "smoke-embedding-space"
    return HarborEmbedClientConfig.from_dict(
        {
            "embed": {
                "default_model": "smoke",
                "timeout_seconds": _timeout(),
                "retry": {
                    "same_deployment_attempts": 1,
                    "max_deployment_failovers": 0,
                    "max_model_fallbacks": 0,
                },
                "models": {
                    "smoke": {
                        "embedding_space": embedding_space,
                        "deployments": [deployment],
                    }
                },
            }
        }
    )


def rerank_config() -> HarborRerankClientConfig:
    return HarborRerankClientConfig.from_dict(
        {
            "rerank": {
                "default_model": "smoke",
                "timeout_seconds": _timeout(),
                "retry": {
                    "same_deployment_attempts": 1,
                    "max_deployment_failovers": 0,
                    "max_model_fallbacks": 0,
                },
                "models": {"smoke": {"deployments": [_deployment("HARBOR_SMOKE_RERANK")]}},
            }
        }
    )
