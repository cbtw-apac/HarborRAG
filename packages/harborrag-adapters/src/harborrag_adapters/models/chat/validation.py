from __future__ import annotations

from harborrag_adapters.models.runtime.config import RoutingEngine, RoutingStrategy
from harborrag_adapters.models.runtime.provider_validation import (
    validate_extension_parameters,
    validate_provider_deployment,
    validate_request_headers,
)
from harborrag_adapters.models.runtime.transport import validate_base_url
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

from .backend_config import ChatBackendType
from .configs import (
    HarborChatClientConfig,
    HarborChatModelConfig,
    HarborChatProviderConfig,
)
from .registry import HarborProvider, ProviderRegistry

_CHAT_TYPED_EXTENSION_FIELDS = frozenset(
    {
        "max_completion_tokens",
        "max_tokens",
        "messages",
        "parallel_tool_calls",
        "reasoning_effort",
        "response_format",
        "seed",
        "stop",
        "temperature",
        "tool_choice",
        "tools",
        "top_p",
        "user",
    }
)


def validate_chat_configuration(
    config: HarborChatClientConfig, registry: ProviderRegistry | None = None
) -> None:
    """Validate provider policy, credentials, endpoints, and enabled chat routes."""

    backend_type = config.backend.resolved_type(config.routing.engine)
    _validate_backend_routing(config, backend_type)
    if (
        config.routing.engine is RoutingEngine.LITELLM_ROUTER
        and config.routing.strategy is RoutingStrategy.ROUND_ROBIN
    ):
        raise HarborChatConfigurationError(
            "LiteLLM Router cannot provide exact round-robin chat routing; "
            "use the direct SDK backend with routing.engine=harbor"
        )
    if config.backend.proxy is not None:
        try:
            validate_base_url(
                config.backend.proxy.api_base,
                allowed_hosts=config.security.allowed_base_url_hosts,
                require_https=config.security.require_https_for_remote_endpoints,
            )
        except ValueError as exc:
            raise HarborChatConfigurationError(str(exc), original_exception=exc) from exc
    active_registry = registry or ProviderRegistry.default()
    for logical_name, logical in config.models.items():
        default_deployment(logical_name, logical)
        for deployment in logical.deployments:
            _validate_backend_provider(backend_type, logical_name, deployment)
            validate_provider_deployment(
                deployment,
                logical_model=logical_name,
                metadata=active_registry.get(deployment.provider),
                policy=config.security,
                error_type=HarborChatConfigurationError,
            )


def _validate_backend_routing(
    config: HarborChatClientConfig, backend_type: ChatBackendType
) -> None:
    expected = (
        RoutingEngine.LITELLM_ROUTER
        if backend_type is ChatBackendType.LITELLM_ROUTER
        else RoutingEngine.HARBOR
    )
    if config.routing.engine is not expected:
        raise HarborChatConfigurationError(
            f"backend {backend_type.value!r} requires routing.engine={expected.value!r}"
        )


def _validate_backend_provider(
    backend_type: ChatBackendType,
    logical_name: str,
    deployment: HarborChatProviderConfig,
) -> None:
    is_proxy_provider = deployment.provider is HarborProvider.LITELLM_PROXY
    if backend_type is ChatBackendType.LITELLM_PROXY and not is_proxy_provider:
        raise HarborChatConfigurationError(
            "LiteLLM Proxy backend deployments must use provider='litellm_proxy'",
            logical_model=logical_name,
            deployment=deployment.name,
        )
    if backend_type is not ChatBackendType.LITELLM_PROXY and is_proxy_provider:
        raise HarborChatConfigurationError(
            "provider='litellm_proxy' requires backend.type='litellm_proxy'",
            logical_model=logical_name,
            deployment=deployment.name,
        )


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
    """Validate chat semantics, declared capabilities, and request security."""

    unsupported = ((request.token_budget is not None, "token budgeting"),)
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
    _validate_reasoning_capability(request, deployment)
    _validate_multimodal_capabilities(request, deployment)
    _validate_response_format_capability(request, deployment)
    if request.tool_choice is not None and not request.tools:
        raise _invalid(request, deployment, "tool_choice requires at least one tool definition")
    if request.parallel_tool_calls is not None and not request.tools:
        raise _invalid(
            request,
            deployment,
            "parallel_tool_calls requires at least one tool definition",
        )
    for message in request.messages:
        if message.role is MessageRole.TOOL and message.tool_call_id is None:
            raise _invalid(request, deployment, "tool messages require tool_call_id")
    _validate_request_security(request, config, deployment)


def _validate_reasoning_capability(
    request: HarborChatRequest, deployment: HarborChatProviderConfig
) -> None:
    if request.reasoning_effort is None:
        return
    if not bool(getattr(deployment.capabilities, "reasoning", False)):
        _raise_capability_error(request, deployment, "reasoning")


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
    raise _invalid(request, deployment, "response_format must use json_schema or json_object")


def _validate_request_security(
    request: HarborChatRequest,
    config: HarborChatClientConfig,
    deployment: HarborChatProviderConfig,
) -> None:
    policy = config.security
    if len(request.extra_params) > policy.max_extra_params:
        raise _invalid(request, deployment, "too many request extra_params")
    try:
        validate_extension_parameters(
            request.extra_params,
            allowed=policy.allowed_extra_litellm_params,
            reserved=_CHAT_TYPED_EXTENSION_FIELDS,
        )
        validate_request_headers(
            request.custom_headers,
            allow_auth_headers=policy.allow_request_auth_headers,
        )
    except ValueError as exc:
        raise _invalid(request, deployment, str(exc), exc) from exc


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


def _invalid(
    request: HarborChatRequest,
    deployment: HarborChatProviderConfig,
    message: str,
    original: Exception | None = None,
) -> HarborChatInvalidRequestError:
    return HarborChatInvalidRequestError(
        message,
        operation="chat",
        provider=deployment.provider.value,
        logical_model=request.logical_model,
        provider_model=deployment.model,
        deployment=deployment.name,
        request_id=request.metadata.request_id,
        retryable=False,
        original_exception=original,
    )
