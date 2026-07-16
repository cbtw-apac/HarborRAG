from __future__ import annotations

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

from harborrag_core.models.common.transport import validate_base_url
from .configs import (
    HarborEmbedClientConfig,
    HarborEmbedModelConfig,
    HarborEmbedProviderConfig,
)
from .registry import HarborEmbedProvider

_AUTH_HEADERS = frozenset({"authorization", "proxy-authorization", "x-api-key", "api-key"})


def validate_embed_configuration(config: HarborEmbedClientConfig) -> None:
    """Validate security and require an enabled route for every model."""
    try:
        for logical_name, logical in config.models.items():
            default_deployment(logical_name, logical)
            for deployment in logical.deployments:
                validate_base_url(
                    deployment.api_base,
                    allowed_hosts=config.security.allowed_base_url_hosts,
                    require_https=config.security.require_https_for_remote_endpoints,
                )
                if (
                    deployment.provider is HarborEmbedProvider.CUSTOM
                    and not config.security.allow_custom_providers
                ):
                    raise ValueError("custom embedding providers are disabled")
                unknown = set(deployment.extra_litellm_params).difference(
                    config.security.allowed_extra_litellm_params
                )
                if unknown:
                    raise ValueError(
                        f"deployment {deployment.name!r} contains disallowed LiteLLM "
                        f"parameters: {', '.join(sorted(unknown))}"
                    )
    except ValueError as exc:
        raise HarborEmbedConfigurationError(str(exc), original_exception=exc) from exc


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
    """Validate security and adapt only semantics-preserving capability differences."""

    if len(request.inputs) > config.max_inputs_per_request:
        raise _invalid(
            request, f"request exceeds max_inputs_per_request={config.max_inputs_per_request}"
        )
    for item in request.inputs:
        if isinstance(item, str) and len(item) > config.max_characters_per_input:
            raise _invalid(request, "embedding input exceeds max_characters_per_input")
    if len(request.extra_params) > config.security.max_extra_params:
        raise _invalid(request, "too many request extra_params")
    unknown = set(request.extra_params).difference(config.security.allowed_extra_litellm_params)
    if unknown:
        raise _invalid(
            request,
            "request contains disallowed LiteLLM parameters: " + ", ".join(sorted(unknown)),
        )
    auth_headers = {name.lower() for name in request.custom_headers}.intersection(_AUTH_HEADERS)
    if auth_headers and not config.security.allow_request_auth_headers:
        raise _invalid(request, "request-level authentication headers are disabled")

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
    if purpose is not None and purpose is not EmbeddingPurpose.UNSPECIFIED:
        if not capabilities.purpose or purpose not in capabilities.supported_purposes:
            raise _capability(request, deployment, f"embedding purpose {purpose.value}")
    return request.model_copy(update=updates) if updates else request


def _invalid(request: HarborEmbedRequest, message: str) -> HarborEmbedInvalidRequestError:
    return HarborEmbedInvalidRequestError(
        message,
        operation="embed",
        logical_model=request.logical_model,
        request_id=request.metadata.request_id,
        retryable=False,
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
