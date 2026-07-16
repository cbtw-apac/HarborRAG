from __future__ import annotations

from harborrag_core.models.chat import (
    HarborChatRequest,
    InputAudioContentPart,
    MessageRole,
)
from harborrag_core.models.errors import (
    HarborChatCapabilityError,
    HarborChatConfigurationError,
    HarborChatInvalidRequestError,
)

from harborrag_adapters.models.common.transport import validate_base_url
from .configs import (
    HarborChatClientConfig,
    HarborChatModelConfig,
    HarborChatProviderConfig,
)


def validate_chat_configuration(config: HarborChatClientConfig) -> None:
    """Validate provider security and require an enabled route for every model."""
    policy = config.security
    for logical_name, logical in config.models.items():
        default_deployment(logical_name, logical)
        for deployment in logical.deployments:
            if (
                policy.allowed_providers is not None
                and deployment.provider not in policy.allowed_providers
            ):
                raise HarborChatConfigurationError(
                    f"provider {deployment.provider.value!r} is not allowed",
                    logical_model=logical_name,
                    deployment=deployment.name,
                )
            if deployment.provider.value == "custom" and not policy.allow_custom_providers:
                raise HarborChatConfigurationError(
                    "custom providers are disabled by security policy",
                    logical_model=logical_name,
                    deployment=deployment.name,
                )
            _validate_deployment_security(config, logical_name, deployment)


def default_deployment(
    logical_name: str, logical: HarborChatModelConfig
) -> HarborChatProviderConfig:
    """Return the deterministic first enabled deployment for request preparation."""

    enabled = [deployment for deployment in logical.deployments if deployment.enabled]
    if not enabled:
        raise HarborChatConfigurationError(
            "chat logical model requires at least one enabled deployment",
            logical_model=logical_name,
        )
    return min(enabled, key=lambda deployment: (deployment.order, deployment.name))


def validate_chat_request(
    request: HarborChatRequest,
    config: HarborChatClientConfig,
    deployment: HarborChatProviderConfig,
) -> None:
    """Validate request semantics, declared capabilities, and request security."""

    unsupported = (
        (request.reasoning_effort is not None, "reasoning parameters"),
        (request.token_budget is not None, "token budgeting"),
    )
    for is_unsupported, feature in unsupported:
        if is_unsupported:
            raise HarborChatCapabilityError(
                f"{feature} is not available in HarborChatClient",
                operation="chat",
                provider=deployment.provider.value,
                logical_model=request.logical_model,
                provider_model=deployment.model,
                deployment=deployment.name,
                request_id=request.metadata.request_id,
                retryable=False,
            )
    _validate_multimodal_capabilities(request, deployment)
    _validate_response_format_capability(request, deployment)
    if request.tool_choice is not None and not request.tools:
        raise HarborChatInvalidRequestError(
            "tool_choice requires at least one tool definition",
            operation="chat",
            provider=deployment.provider.value,
            logical_model=request.logical_model,
            provider_model=deployment.model,
            deployment=deployment.name,
            request_id=request.metadata.request_id,
            retryable=False,
        )
    if request.parallel_tool_calls is not None and not request.tools:
        raise HarborChatInvalidRequestError(
            "parallel_tool_calls requires at least one tool definition",
            operation="chat",
            provider=deployment.provider.value,
            logical_model=request.logical_model,
            provider_model=deployment.model,
            deployment=deployment.name,
            request_id=request.metadata.request_id,
            retryable=False,
        )
    for message in request.messages:
        if message.role is MessageRole.TOOL and message.tool_call_id is None:
            raise HarborChatInvalidRequestError(
                "tool messages require tool_call_id",
                operation="chat",
                logical_model=request.logical_model,
                request_id=request.metadata.request_id,
                retryable=False,
            )
    _validate_request_security(request, config)


def _validate_multimodal_capabilities(
    request: HarborChatRequest,
    deployment: HarborChatProviderConfig,
) -> None:
    content_parts = tuple(
        part
        for message in request.messages
        if isinstance(message.content, tuple)
        for part in message.content
    )
    if content_parts and not deployment.capabilities.multimodal:
        _raise_capability_error(request, deployment, "multimodal messages")
    if any(isinstance(part, InputAudioContentPart) for part in content_parts) and not (
        deployment.capabilities.audio_input
    ):
        _raise_capability_error(request, deployment, "audio input")


def _validate_response_format_capability(
    request: HarborChatRequest,
    deployment: HarborChatProviderConfig,
) -> None:
    response_format = request.response_format
    if response_format is None:
        return
    if isinstance(response_format, type):
        if not deployment.capabilities.structured_output:
            _raise_capability_error(request, deployment, "native structured output")
        return
    format_type = response_format.get("type")
    if format_type == "json_schema" and isinstance(response_format.get("json_schema"), dict):
        if not deployment.capabilities.structured_output:
            _raise_capability_error(request, deployment, "native structured output")
        return
    if format_type == "json_object":
        if not deployment.capabilities.json_mode:
            _raise_capability_error(request, deployment, "JSON response mode")
        return
    raise HarborChatInvalidRequestError(
        "response_format must use json_schema or json_object",
        operation="chat",
        provider=deployment.provider.value,
        logical_model=request.logical_model,
        provider_model=deployment.model,
        deployment=deployment.name,
        request_id=request.metadata.request_id,
        retryable=False,
    )


def _raise_capability_error(
    request: HarborChatRequest,
    deployment: HarborChatProviderConfig,
    feature: str,
) -> None:
    raise HarborChatCapabilityError(
        f"deployment does not declare support for {feature}",
        operation="chat",
        provider=deployment.provider.value,
        logical_model=request.logical_model,
        provider_model=deployment.model,
        deployment=deployment.name,
        request_id=request.metadata.request_id,
        retryable=False,
    )


def _validate_deployment_security(
    config: HarborChatClientConfig,
    logical_name: str,
    deployment: HarborChatProviderConfig,
) -> None:
    policy = config.security
    disallowed = set(deployment.extra_litellm_params) - set(policy.allowed_extra_litellm_params)
    if disallowed:
        raise HarborChatConfigurationError(
            f"unsafe or unapproved LiteLLM params: {', '.join(sorted(disallowed))}",
            logical_model=logical_name,
            deployment=deployment.name,
        )
    if deployment.api_base:
        try:
            validate_base_url(
                deployment.api_base,
                allowed_hosts=policy.allowed_base_url_hosts,
                require_https=policy.require_https_for_remote_endpoints,
            )
        except ValueError as exc:
            raise HarborChatConfigurationError(
                str(exc),
                logical_model=logical_name,
                deployment=deployment.name,
                original_exception=exc,
            ) from exc


def _validate_request_security(request: HarborChatRequest, config: HarborChatClientConfig) -> None:
    policy = config.security
    if len(request.extra_params) > policy.max_extra_params:
        raise HarborChatConfigurationError(
            "too many request extra_params",
            logical_model=request.logical_model,
            request_id=request.metadata.request_id,
        )
    disallowed = set(request.extra_params) - set(policy.allowed_extra_litellm_params)
    if disallowed:
        raise HarborChatConfigurationError(
            f"unsafe or unapproved request LiteLLM params: {', '.join(sorted(disallowed))}",
            logical_model=request.logical_model,
            request_id=request.metadata.request_id,
        )
    auth_headers = {
        name
        for name in request.custom_headers
        if name.lower() in {"authorization", "proxy-authorization", "x-api-key", "api-key"}
    }
    if auth_headers and not policy.allow_request_auth_headers:
        raise HarborChatConfigurationError(
            f"request-level authentication headers are disabled: {', '.join(sorted(auth_headers))}",
            logical_model=request.logical_model,
            request_id=request.metadata.request_id,
        )
