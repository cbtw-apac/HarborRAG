from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from bootstrap import (
    SmokeConfigurationError,
    env,
    env_bool,
    env_int,
    require_env,
)
from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_adapters.repositories.graph.falkordb import FalkorDBGraphConfig
from harborrag_adapters.repositories.vector.qdrant import QdrantVectorConfig
from harborrag_engine.ingestion.indexing import IndexingConfig
from pydantic import SecretStr


def embedding_config() -> HarborEmbedClientConfig:
    """Build the real provider-neutral embedding client configuration."""

    dimensions = env_int("HARBOR_EMBED_EXPECTED_DIMENSIONS")
    deployment: dict[str, Any] = {
        "name": "smoke",
        "provider": require_env("HARBOR_EMBED_PROVIDER"),
        "model": require_env("HARBOR_EMBED_MODEL"),
        "expected_dimensions": dimensions,
        "allow_ambient_credentials": env_bool(
            "HARBOR_EMBED_ALLOW_AMBIENT_CREDENTIALS"
        ),
        "capabilities": {
            "batch": True,
            "configurable_dimensions": env_bool(
                "HARBOR_EMBED_CONFIGURABLE_DIMENSIONS"
            ),
            "default_dimensions": dimensions,
            "encoding_format": True,
            "purpose": True,
            "supported_purposes": ["document"],
        },
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
        value = env(f"HARBOR_EMBED_{suffix}")
        if value is not None:
            deployment[field] = value
    headers = _json_mapping("HARBOR_EMBED_HEADERS_JSON")
    if headers:
        deployment["headers"] = headers
    extra = _json_mapping("HARBOR_EMBED_EXTRA_LITELLM_PARAMS_JSON")
    if extra:
        deployment["extra_litellm_params"] = extra

    return HarborEmbedClientConfig.from_dict(
        {
            "embed": {
                "default_model": "smoke",
                "security": {
                    "allow_custom_providers": env_bool(
                        "HARBOR_EMBED_ALLOW_CUSTOM_PROVIDER"
                    )
                },
                "retry": {
                    "same_deployment_attempts": 1,
                    "max_deployment_failovers": 0,
                    "max_model_fallbacks": 0,
                },
                "models": {
                    "smoke": {
                        "embedding_space": env(
                            "HARBOR_EMBED_SPACE", "smoke-embedding-space"
                        ),
                        "deployments": [deployment],
                    }
                },
            }
        }
    )


def qdrant_config() -> QdrantVectorConfig:
    """Build a remote Qdrant configuration for the real smoke run."""

    url = env("HARBOR_SMOKE_QDRANT_URL")
    if url is None:
        url = f"http://127.0.0.1:{env_int('QDRANT_HTTP_PORT', 6333)}"
    api_key = env("HARBOR_SMOKE_QDRANT_API_KEY")
    return QdrantVectorConfig(
        instance_name="engine-indexing-smoke",
        url=url,
        api_key=SecretStr(api_key) if api_key else None,
        prefer_grpc=env_bool("HARBOR_SMOKE_QDRANT_PREFER_GRPC"),
        collection_prefix=env("HARBOR_SMOKE_QDRANT_PREFIX", "harborrag_smoke_") or "",
    )


def falkordb_config() -> FalkorDBGraphConfig:
    """Build a FalkorDB configuration for the real smoke run."""

    password = env("FALKORDB_PASSWORD")
    return FalkorDBGraphConfig(
        instance_name="engine-indexing-smoke",
        host=env("FALKORDB_HOST", "127.0.0.1") or "127.0.0.1",
        port=env_int("FALKORDB_PORT", 6379),
        username=env("FALKORDB_USERNAME"),
        password=SecretStr(password) if password else None,
        graph_name=env(
            "HARBOR_SMOKE_INDEXING_FALKORDB_GRAPH", "harborrag_indexing_smoke"
        )
        or "harborrag_indexing_smoke",
        ssl=env_bool("FALKORDB_SSL"),
    )


def indexing_config() -> IndexingConfig:
    """Build engine indexing policy aligned with the live embedding model."""

    return IndexingConfig(
        embedding_model="smoke",
        embedding_dimensions=env_int("HARBOR_EMBED_EXPECTED_DIMENSIONS"),
        vector_collection=env(
            "HARBOR_SMOKE_INDEXING_QDRANT_COLLECTION", "indexing_probe"
        )
        or "indexing_probe",
        graph_namespace=env("HARBOR_SMOKE_INDEXING_GRAPH_NAMESPACE", "smoke")
        or "smoke",
        embedding_batch_size=2,
        embedding_concurrency=1,
    )


def _json_mapping(name: str) -> dict[str, Any]:
    value = env(name)
    if value is None:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SmokeConfigurationError(f"{name} must contain valid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise SmokeConfigurationError(f"{name} must decode to a JSON object")
    return dict(decoded)
