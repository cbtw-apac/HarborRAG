from __future__ import annotations

import os
from typing import Any, Literal

from .config import RoutingStrategy
from .provider import ProviderDeploymentConfig, ProviderMetadata
from .security import reveal_secret
from .transport import reveal_headers

type LiteLLMRoutingStrategy = Literal[
    "simple-shuffle",
    "least-busy",
    "latency-based-routing",
]

NO_API_KEY_PLACEHOLDER = "harborrag-no-api-key"
"""Key sent when a deployment sets no ``api_key``. It is fixed and public, never a secret."""


def configure_litellm() -> None:
    """Import LiteLLM with its packaged cost map, no stdout banner and no SDK retries.

    LiteLLM reads its cost map once, on first import, so call this before any other
    LiteLLM import. An operator's own ``LITELLM_LOCAL_MODEL_COST_MAP`` is kept.
    Exceptions leave out request messages, and SDK retries stay off because
    Harbor's routing executor owns retries.
    """

    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    import litellm

    litellm.suppress_debug_info = True
    litellm.redact_messages_in_exceptions = True
    litellm.DEFAULT_MAX_RETRIES = 0  # type: ignore[misc]  # read per embedding call, as `max_retries or DEFAULT_MAX_RETRIES`


def build_provider_params(
    deployment: ProviderDeploymentConfig,
    *,
    metadata: ProviderMetadata,
    model: str | None = None,
) -> dict[str, Any]:
    """Translate shared deployment fields into LiteLLM keyword arguments.

    ``max_retries`` is 0 so the provider SDK never retries under Harbor's own
    retry and fallback policy.

    Args:
        deployment: The deployment to call.
        metadata: Its provider's registry entry, which names the LiteLLM provider
            and decides whether a missing ``api_key`` becomes
            ``NO_API_KEY_PLACEHOLDER``.
        model: Model name to send instead of the deployment's own, such as a
            LiteLLM Router group.

    Returns:
        LiteLLM keyword arguments.
    """

    params: dict[str, Any] = {"model": model or deployment.model, "max_retries": 0}
    optional = {
        "api_key": _api_key(deployment, metadata),
        "api_base": deployment.api_base,
        "api_version": deployment.api_version,
        "custom_llm_provider": deployment.custom_llm_provider or metadata.litellm_provider,
        "aws_region_name": deployment.aws_region_name,
        "aws_access_key_id": reveal_secret(deployment.aws_access_key_id),
        "aws_secret_access_key": reveal_secret(deployment.aws_secret_access_key),
        "aws_session_token": reveal_secret(deployment.aws_session_token),
        "aws_role_name": deployment.aws_role_name,
        "aws_role_session_name": deployment.aws_role_session_name,
        "vertex_project": deployment.vertex_project,
        "vertex_location": deployment.vertex_location,
        "vertex_credentials": reveal_secret(deployment.vertex_credentials),
    }
    params.update({key: value for key, value in optional.items() if value is not None})
    headers = reveal_headers(deployment.headers)
    if headers:
        params["extra_headers"] = headers
    params.update(deployment.extra_litellm_params)
    return params


def _api_key(deployment: ProviderDeploymentConfig, metadata: ProviderMetadata) -> str | None:
    """Return the deployment's key, or ``NO_API_KEY_PLACEHOLDER`` in place of an ambient one.

    A provider that neither requires a key nor accepts ambient credentials may run
    without one. LiteLLM would then read a key such as ``OPENAI_API_KEY`` from the
    environment and send it to ``api_base``. A keyless server ignores the placeholder,
    and a server that needs a key refuses it.
    """
    key = reveal_secret(deployment.api_key)
    if key is None and not (metadata.requires_api_key or metadata.supports_ambient_credentials):
        return NO_API_KEY_PLACEHOLDER
    return key


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
