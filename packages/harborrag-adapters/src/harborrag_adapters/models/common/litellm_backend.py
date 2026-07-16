from __future__ import annotations

from typing import Any, Literal

from .config import RoutingStrategy
from .provider import ProviderDeploymentConfig
from .security import reveal_secret
from .transport import reveal_headers

type LiteLLMRoutingStrategy = Literal[
    "simple-shuffle",
    "least-busy",
    "latency-based-routing",
]


def build_provider_params(
    deployment: ProviderDeploymentConfig,
    *,
    litellm_provider: str | None,
    model: str | None = None,
) -> dict[str, Any]:
    """Translate shared deployment fields into LiteLLM keyword arguments."""

    params: dict[str, Any] = {"model": model or deployment.model}
    optional = {
        "api_key": reveal_secret(deployment.api_key),
        "api_base": deployment.api_base,
        "api_version": deployment.api_version,
        "custom_llm_provider": deployment.custom_llm_provider or litellm_provider,
        "aws_region_name": deployment.aws_region_name,
        "aws_access_key_id": reveal_secret(deployment.aws_access_key_id),
        "aws_secret_access_key": reveal_secret(deployment.aws_secret_access_key),
        "aws_session_token": reveal_secret(deployment.aws_session_token),
        "aws_role_name": deployment.aws_role_name,
        "aws_role_session_name": deployment.aws_role_session_name,
        "vertex_project": deployment.vertex_project,
        "vertex_location": deployment.vertex_location,
    }
    params.update({key: value for key, value in optional.items() if value is not None})
    headers = reveal_headers(deployment.headers)
    if headers:
        params["extra_headers"] = headers
    params.update(deployment.extra_litellm_params)
    return params


def litellm_routing_strategy(strategy: RoutingStrategy) -> LiteLLMRoutingStrategy:
    """Translate Harbor routing strategy names into LiteLLM Router literals."""

    mapping: dict[RoutingStrategy, LiteLLMRoutingStrategy] = {
        RoutingStrategy.WEIGHTED: "simple-shuffle",
        RoutingStrategy.ORDERED: "simple-shuffle",
        RoutingStrategy.LEAST_BUSY: "least-busy",
        RoutingStrategy.LATENCY: "latency-based-routing",
    }
    try:
        return mapping[strategy]
    except KeyError as exc:
        raise ValueError(f"unsupported LiteLLM routing strategy: {strategy.value}") from exc
