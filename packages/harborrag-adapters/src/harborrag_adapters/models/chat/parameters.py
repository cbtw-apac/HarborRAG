from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest
from harborrag_core.models.errors import (
    HarborChatConfigurationError,
    HarborChatInvalidRequestError,
)
from pydantic import ValidationError

from harborrag_core.models.common.litellm_backend import build_provider_params
from harborrag_core.models.common.security import reveal_secret
from .configs import (
    HarborChatClientConfig,
    HarborChatModelConfig,
    HarborChatProviderConfig,
)
from .registry import HarborProvider, ProviderRegistry
from .validation import default_deployment, validate_chat_request

_RESERVED_REQUEST_PARAMETERS = frozenset(
    {
        "api_base",
        "api_key",
        "api_version",
        "custom_llm_provider",
        "extra_headers",
        "max_completion_tokens",
        "max_tokens",
        "messages",
        "model",
        "seed",
        "stop",
        "stream",
        "stream_options",
        "temperature",
        "timeout",
        "tool_choice",
        "tools",
        "top_p",
        "user",
        "parallel_tool_calls",
        "response_format",
    }
)

type ChatMessageInput = HarborChatMessage | Mapping[str, Any]


def prepare_chat_request(
    config: HarborChatClientConfig,
    messages: Sequence[ChatMessageInput] | None,
    *,
    request: HarborChatRequest | None,
    model: str | None,
    request_kwargs: Mapping[str, Any],
) -> tuple[str, HarborChatProviderConfig, HarborChatRequest]:
    """Build, default, identify, and validate one chat operation request."""

    try:
        prepared = build_chat_request(
            messages,
            request=request,
            model=model,
            request_kwargs=request_kwargs,
        )
    except ValidationError as exc:
        raise HarborChatInvalidRequestError(
            "invalid chat request parameters",
            operation="chat",
            logical_model=model,
            original_exception=exc,
        ) from exc
    try:
        logical_name, logical = config.model_for(prepared.logical_model)
    except KeyError as exc:
        raise HarborChatConfigurationError(
            str(exc),
            operation="chat",
            logical_model=prepared.logical_model,
            request_id=prepared.metadata.request_id,
            original_exception=exc,
        ) from exc
    prepared = apply_generation_defaults(prepared, logical_name, logical)
    prepared = ensure_request_id(prepared)
    deployment = default_deployment(logical_name, logical)
    validate_chat_request(prepared, config, deployment)
    return logical_name, deployment, prepared


def build_chat_request(
    messages: Sequence[ChatMessageInput] | None,
    *,
    request: HarborChatRequest | None,
    model: str | None,
    request_kwargs: Mapping[str, Any],
) -> HarborChatRequest:
    """Build one core request from either messages or an existing request object."""

    if request is not None and messages is not None:
        raise HarborChatInvalidRequestError(
            "messages and request are mutually exclusive", operation="chat"
        )
    if request is not None and request_kwargs:
        names = ", ".join(sorted(request_kwargs))
        raise HarborChatInvalidRequestError(
            f"request cannot be combined with keyword parameters: {names}",
            operation="chat",
            logical_model=request.logical_model,
            request_id=request.metadata.request_id,
        )
    if request is not None:
        if model is None:
            return request
        return HarborChatRequest.model_validate(
            {**request.model_dump(mode="python"), "logical_model": model}
        )
    if messages is None:
        raise HarborChatInvalidRequestError(
            "messages or request is required", operation="chat", logical_model=model
        )
    normalized_messages = tuple(
        message
        if isinstance(message, HarborChatMessage)
        else HarborChatMessage.model_validate(message)
        for message in messages
    )
    return HarborChatRequest(
        messages=normalized_messages,
        logical_model=model,
        **request_kwargs,
    )


def apply_generation_defaults(
    request: HarborChatRequest,
    logical_name: str,
    logical: HarborChatModelConfig,
) -> HarborChatRequest:
    """Apply logical-model defaults only where a request omitted a value."""

    values = request.model_dump(mode="python")
    values["logical_model"] = logical_name
    for name, default in logical.default_params.model_dump(exclude_none=True).items():
        if values.get(name) is None:
            values[name] = default
    return HarborChatRequest.model_validate(values)


def ensure_request_id(request: HarborChatRequest) -> HarborChatRequest:
    """Return a request carrying a stable generated identity when necessary."""

    if request.metadata.request_id is not None:
        return request
    metadata = request.metadata.model_copy(update={"request_id": str(uuid4())})
    return request.model_copy(update={"metadata": metadata})


def chat_request_id(request: HarborChatRequest) -> str:
    """Return the identity guaranteed by request preparation."""

    request_id = request.metadata.request_id
    if request_id is None:
        raise RuntimeError("prepared chat request has no request ID")
    return request_id


def build_litellm_parameters(
    deployment: HarborChatProviderConfig,
    request: HarborChatRequest,
    *,
    timeout: float,
    stream: bool = False,
    model_override: str | None = None,
) -> dict[str, Any]:
    """Translate one Harbor request into LiteLLM completion parameters."""

    conflicts = _RESERVED_REQUEST_PARAMETERS.intersection(request.extra_params)
    if conflicts:
        names = ", ".join(sorted(conflicts))
        raise HarborChatInvalidRequestError(
            f"extra_params cannot replace normalized request parameters: {names}",
            operation="chat",
            logical_model=request.logical_model,
            request_id=request.metadata.request_id,
        )
    descriptor = ProviderRegistry.default().get(deployment.provider)
    provider_model = (
        f"azure/{deployment.deployment_name}"
        if deployment.provider is HarborProvider.AZURE_OPENAI and deployment.deployment_name
        else deployment.model
    )
    parameters = build_provider_params(
        deployment,
        litellm_provider=descriptor.litellm_provider,
        model=model_override or provider_model,
    )
    deployment_headers = parameters.pop("extra_headers", {})
    request_headers = {key: reveal_secret(value) for key, value in request.custom_headers.items()}
    parameters.update(
        {
            "messages": build_litellm_messages(request),
            "timeout": timeout,
            "stream": stream,
        }
    )
    optionals = {
        "temperature": request.temperature,
        "top_p": request.top_p,
        "max_tokens": request.max_tokens,
        "max_completion_tokens": request.max_completion_tokens,
        "stop": list(request.stop) if isinstance(request.stop, tuple) else request.stop,
        "seed": request.seed,
        "user": request.metadata.user_id,
    }
    parameters.update({name: value for name, value in optionals.items() if value is not None})
    if request.tools:
        parameters["tools"] = [
            tool.model_dump(mode="json", exclude_none=True) for tool in request.tools
        ]
    if request.tool_choice is not None:
        parameters["tool_choice"] = request.tool_choice
    if request.parallel_tool_calls is not None:
        parameters["parallel_tool_calls"] = request.parallel_tool_calls
    if request.response_format is not None:
        parameters["response_format"] = request.response_format
    if stream:
        parameters["stream_options"] = {"include_usage": True}
    parameters.update(request.extra_params)
    headers = {**deployment_headers, **request_headers}
    if headers:
        parameters["extra_headers"] = headers
    return parameters


def build_litellm_messages(request: HarborChatRequest) -> list[dict[str, Any]]:
    """Render validated text, image, audio, and tool messages as LiteLLM dictionaries."""

    messages: list[dict[str, Any]] = []
    for message in request.messages:
        rendered: dict[str, Any] = {"role": message.role.value}
        if isinstance(message.content, str):
            rendered["content"] = message.content
        elif isinstance(message.content, tuple):
            rendered["content"] = [
                part.model_dump(mode="json", exclude_none=True) for part in message.content
            ]
        if message.name is not None:
            rendered["name"] = message.name
        if message.tool_call_id is not None:
            rendered["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            rendered["tool_calls"] = [
                {
                    "id": call.id,
                    "type": call.type,
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in message.tool_calls
            ]
        messages.append(rendered)
    return messages
