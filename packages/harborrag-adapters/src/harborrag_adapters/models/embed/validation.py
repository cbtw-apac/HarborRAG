from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from harborrag_adapters.models.runtime.config import RoutingEngine, RoutingStrategy
from harborrag_adapters.models.runtime.provider_validation import (
    validate_extension_parameters,
    validate_provider_deployment,
    validate_request_headers,
)
from harborrag_adapters.models.runtime.security import HeaderValue
from harborrag_core.models.embed import (
    EmbeddingEncodingFormat,
    EmbeddingPurpose,
    HarborEmbedRequest,
)
from harborrag_core.models.errors import (
    HarborEmbedCapabilityError,
    HarborEmbedConfigurationError,
    HarborEmbedInvalidRequestError,
)

from .configs import (
    HarborEmbedClientConfig,
    HarborEmbedModelConfig,
    HarborEmbedProviderConfig,
)
from .registry import EmbedProviderRegistry

_EMBED_TYPED_EXTENSION_FIELDS = frozenset(
    {"dimensions", "encoding_format", "input", "model", "timeout", "user"}
)


def validate_embed_configuration(
    config: HarborEmbedClientConfig, registry: EmbedProviderRegistry | None = None
) -> None:
    """Validate provider policy, credentials, endpoints, and enabled embedding routes."""

    if (
        config.routing.engine is RoutingEngine.LITELLM_ROUTER
        and config.routing.strategy is RoutingStrategy.ROUND_ROBIN
    ):
        raise HarborEmbedConfigurationError(
            "LiteLLM Router cannot provide exact round-robin embedding routing; "
            "use routing.engine=harbor"
        )
    active_registry = registry or EmbedProviderRegistry.default()
    for logical_name, logical in config.models.items():
        default_deployment(logical_name, logical)
        for deployment in logical.deployments:
            validate_provider_deployment(
                deployment,
                logical_model=logical_name,
                metadata=active_registry.get(deployment.provider),
                policy=config.security,
                error_type=HarborEmbedConfigurationError,
            )


def default_deployment(
    logical_name: str,
    logical: HarborEmbedModelConfig,
) -> HarborEmbedProviderConfig:
    """Return the deterministic first enabled deployment for request preparation."""

    enabled = [deployment for deployment in logical.deployments if deployment.enabled]
    if not enabled:
        raise HarborEmbedConfigurationError(
            "embedding logical model requires at least one enabled deployment",
            logical_model=logical_name,
        )
    return min(enabled, key=lambda deployment: (deployment.order, deployment.name))


def validate_embed_request(
    request: HarborEmbedRequest,
    config: HarborEmbedClientConfig,
    deployment: HarborEmbedProviderConfig,
) -> HarborEmbedRequest:
    """Validate request security and adapt only semantics-preserving capabilities."""

    _validate_request_limits(request, config)
    _validate_request_extensions(request, config)
    updates = _capability_updates(request, deployment)
    return request.model_copy(update=updates) if updates else request


def _validate_request_limits(
    request: HarborEmbedRequest,
    config: HarborEmbedClientConfig,
) -> None:
    if len(request.inputs) > config.max_inputs_per_request:
        raise _invalid(
            request,
            f"request exceeds max_inputs_per_request={config.max_inputs_per_request}",
        )
    for item in request.inputs:
        if isinstance(item, str) and len(item) > config.max_characters_per_input:
            raise _invalid(request, "embedding input exceeds max_characters_per_input")
    if len(request.extra_params) > config.security.max_extra_params:
        raise _invalid(request, "too many request extra_params")


def _validate_request_extensions(
    request: HarborEmbedRequest,
    config: HarborEmbedClientConfig,
) -> None:
    try:
        validate_extension_parameters(
            request.extra_params,
            allowed=config.security.allowed_extra_litellm_params,
            reserved=_EMBED_TYPED_EXTENSION_FIELDS,
        )
        validate_request_headers(
            cast(Mapping[str, HeaderValue], request.custom_headers),
            allow_auth_headers=config.security.allow_request_auth_headers,
        )
    except ValueError as exc:
        raise _invalid(request, str(exc), exc) from exc


def _capability_updates(
    request: HarborEmbedRequest,
    deployment: HarborEmbedProviderConfig,
) -> dict[str, object]:
    capabilities = deployment.capabilities
    if any(isinstance(item, tuple) for item in request.inputs) and not capabilities.token_inputs:
        raise _capability(request, deployment, "token-array embedding inputs")
    updates: dict[str, object] = {}
    if request.dimensions is not None and not capabilities.configurable_dimensions:
        expected = deployment.expected_dimensions or capabilities.default_dimensions
        if request.dimensions == expected:
            updates["dimensions"] = None
        else:
            raise _capability(request, deployment, "configurable embedding dimensions")
    if request.encoding_format is not None and not capabilities.encoding_format:
        if request.encoding_format is EmbeddingEncodingFormat.FLOAT:
            updates["encoding_format"] = None
        else:
            raise _capability(request, deployment, "base64 embedding output")
    purpose = request.purpose
    if (
        purpose is not None
        and purpose is not EmbeddingPurpose.UNSPECIFIED
        and (not capabilities.purpose or purpose not in capabilities.supported_purposes)
    ):
        raise _capability(request, deployment, f"embedding purpose {purpose.value}")
    return updates


def _invalid(
    request: HarborEmbedRequest,
    message: str,
    original: Exception | None = None,
) -> HarborEmbedInvalidRequestError:
    return HarborEmbedInvalidRequestError(
        message,
        operation="embed",
        logical_model=request.logical_model,
        request_id=request.metadata.request_id,
        retryable=False,
        original_exception=original,
    )


def _capability(
    request: HarborEmbedRequest,
    deployment: HarborEmbedProviderConfig,
    feature: str,
) -> HarborEmbedCapabilityError:
    return HarborEmbedCapabilityError(
        f"deployment does not support {feature}",
        operation="embed",
        provider=deployment.provider.value,
        logical_model=request.logical_model,
        provider_model=deployment.model,
        deployment=deployment.name,
        request_id=request.metadata.request_id,
        retryable=False,
    )
