from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from harborrag_core.models.chat import (
    FinishReason,
    HarborChatMessage,
    HarborChatResponse,
    HarborChatUsage,
    HarborToolCall,
    HarborToolCallFunction,
    MessageRole,
)
from harborrag_core.models.errors import HarborChatProviderError

from harborrag_adapters.models.common.responses import coerce_sdk_mapping as coerce_mapping
from harborrag_adapters.models.common.responses import sdk_hidden_parameters
from .configs import HarborChatProviderConfig

_FINISH_REASON_ALIASES = {
    "stop": FinishReason.STOP,
    "eos": FinishReason.STOP,
    "end_turn": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "max_tokens": FinishReason.LENGTH,
    "max_output_tokens": FinishReason.LENGTH,
    "tool_calls": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,
    "content_filter": FinishReason.CONTENT_FILTER,
    "content_filtered": FinishReason.CONTENT_FILTER,
    "safety": FinishReason.CONTENT_FILTER,
    "error": FinishReason.ERROR,
}


def normalize_chat_response(
    raw: Any,
    *,
    logical_model: str,
    deployment: HarborChatProviderConfig,
    request_id: str,
    latency_ms: float,
) -> HarborChatResponse:
    """Convert a LiteLLM response into HarborRAG's stable chat response."""

    data = _response_mapping(raw)
    choice = _first_choice(data, deployment, logical_model, request_id)
    message_data = coerce_mapping(choice.get("message"))
    if not message_data:
        raise _malformed("missing assistant message", deployment, logical_model, request_id)
    role = message_data.get("role", MessageRole.ASSISTANT.value)
    if role != MessageRole.ASSISTANT.value:
        raise _malformed(
            "response message role must be assistant",
            deployment,
            logical_model,
            request_id,
        )
    content = message_data.get("content")
    if content is not None and not isinstance(content, str):
        raise _malformed(
            "assistant content must be text or null",
            deployment,
            logical_model,
            request_id,
        )
    hidden = sdk_hidden_parameters(raw, data)
    provider_model = str(data.get("model") or deployment.model)
    tool_calls = normalize_tool_calls(
        message_data.get("tool_calls"),
        legacy_function_call=message_data.get("function_call"),
    )
    return HarborChatResponse(
        id=str(data.get("id") or request_id),
        created=_optional_int(data.get("created"), "created"),
        logical_model=logical_model,
        provider=deployment.provider.value,
        provider_model=provider_model,
        deployment=deployment.name,
        message=HarborChatMessage.assistant(content, tool_calls=tool_calls),
        finish_reason=normalize_finish_reason(choice.get("finish_reason")),
        usage=normalize_chat_usage(data.get("usage")),
        latency_ms=latency_ms,
        request_id=request_id,
        provider_request_id=_provider_request_id(hidden),
        cache_hit=bool(hidden.get("cache_hit")),
        provider_metadata=_safe_provider_metadata(hidden),
    )


def normalize_chat_usage(raw: Any) -> HarborChatUsage:
    """Normalize OpenAI-style and input/output-style token usage fields."""

    data = coerce_mapping(raw)
    prompt = _token_count(data, "prompt_tokens", fallback="input_tokens")
    completion = _token_count(data, "completion_tokens", fallback="output_tokens")
    total_value = data.get("total_tokens")
    total = (
        prompt + completion
        if total_value is None
        else _nonnegative_int(total_value, "total_tokens")
    )
    prompt_details = coerce_mapping(data.get("prompt_tokens_details"))
    completion_details = coerce_mapping(data.get("completion_tokens_details"))
    return HarborChatUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cache_read_input_tokens=_optional_nonnegative_int(
            prompt_details.get("cached_tokens", data.get("cache_read_input_tokens")),
            "cache_read_input_tokens",
        ),
        cache_creation_input_tokens=_optional_nonnegative_int(
            data.get("cache_creation_input_tokens"),
            "cache_creation_input_tokens",
        ),
        reasoning_tokens=_optional_nonnegative_int(
            completion_details.get("reasoning_tokens", data.get("reasoning_tokens")),
            "reasoning_tokens",
        ),
    )


def normalize_finish_reason(value: Any) -> FinishReason:
    """Map provider finish-reason variants into HarborRAG's stable enum."""

    if value is None:
        return FinishReason.UNKNOWN
    return _FINISH_REASON_ALIASES.get(str(value).lower(), FinishReason.UNKNOWN)


def normalize_tool_calls(
    raw: Any,
    *,
    legacy_function_call: Any = None,
) -> tuple[HarborToolCall, ...]:
    """Normalize complete LiteLLM tool calls and parse object arguments."""

    values = raw
    if values is None and coerce_mapping(legacy_function_call):
        values = [
            {
                "id": "function_call",
                "type": "function",
                "function": legacy_function_call,
            }
        ]
    if values is None:
        return ()
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise HarborChatProviderError("malformed provider response: invalid tool_calls")
    return tuple(_normalize_tool_call(value, position) for position, value in enumerate(values))


def normalize_tool_call_delta(raw: Any, *, fallback_index: int) -> HarborToolCall:
    """Normalize one possibly partial tool-call delta without losing fragments."""

    data = coerce_mapping(raw)
    if not data:
        raise HarborChatProviderError("malformed provider response: invalid tool-call delta")
    function = coerce_mapping(data.get("function"))
    index = _optional_int(data.get("index"), "tool-call index")
    arguments, parsed = _tool_arguments(function.get("arguments"))
    return HarborToolCall(
        id=str(data.get("id") or ""),
        type=str(data.get("type") or "function"),
        index=fallback_index if index is None else index,
        function=HarborToolCallFunction(
            name=str(function.get("name") or ""),
            arguments=arguments,
            parsed_arguments=parsed,
        ),
    )


def parse_tool_arguments(arguments: str) -> dict[str, Any] | None:
    """Parse complete JSON object arguments while preserving malformed input."""

    if not arguments:
        return None
    try:
        value = json.loads(arguments)
    except (TypeError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _normalize_tool_call(raw: Any, fallback_index: int) -> HarborToolCall:
    data = coerce_mapping(raw)
    function = coerce_mapping(data.get("function"))
    call_id = data.get("id")
    name = function.get("name")
    if not data or not call_id or not name:
        raise HarborChatProviderError("malformed provider response: incomplete tool call")
    index = _optional_int(data.get("index"), "tool-call index")
    arguments, parsed = _tool_arguments(function.get("arguments"))
    return HarborToolCall(
        id=str(call_id),
        type=str(data.get("type") or "function"),
        index=fallback_index if index is None else index,
        function=HarborToolCallFunction(
            name=str(name),
            arguments=arguments,
            parsed_arguments=parsed,
        ),
    )


def _tool_arguments(value: Any) -> tuple[str, dict[str, Any] | None]:
    if value is None:
        return "", None
    if isinstance(value, Mapping):
        parsed = dict(value)
        return json.dumps(parsed, separators=(",", ":")), parsed
    if not isinstance(value, str):
        raise HarborChatProviderError("malformed provider response: invalid tool arguments")
    return value, parse_tool_arguments(value)


def _first_choice(
    data: Mapping[str, Any],
    deployment: HarborChatProviderConfig,
    logical_model: str,
    request_id: str,
) -> Mapping[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        raise _malformed("missing choices", deployment, logical_model, request_id)
    choice = coerce_mapping(choices[0])
    if not choice:
        raise _malformed("invalid first choice", deployment, logical_model, request_id)
    return choice


def _response_mapping(value: Any) -> dict[str, Any]:
    data = coerce_mapping(value)
    if not data:
        raise HarborChatProviderError(
            "malformed provider response: expected a response mapping",
            operation="chat",
            retryable=False,
        )
    return data


def _token_count(data: Mapping[str, Any], name: str, *, fallback: str) -> int:
    value = data.get(name, data.get(fallback, 0))
    return _nonnegative_int(value, name)


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise HarborChatProviderError(f"malformed provider response: invalid {name}")
    try:
        result = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise HarborChatProviderError(
            f"malformed provider response: invalid {name}", original_exception=exc
        ) from exc
    if result < 0:
        raise HarborChatProviderError(f"malformed provider response: invalid {name}")
    return result


def _optional_nonnegative_int(value: Any, name: str) -> int | None:
    return None if value is None else _nonnegative_int(value, name)


def _optional_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HarborChatProviderError(
            f"malformed provider response: invalid {name}", original_exception=exc
        ) from exc


def _provider_request_id(hidden: Mapping[str, Any]) -> str | None:
    headers = hidden.get("additional_headers") or hidden.get("headers")
    if isinstance(headers, Mapping):
        value = headers.get("x-request-id") or headers.get("request-id")
        return str(value) if value else None
    return None


def _safe_provider_metadata(hidden: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"model_id", "custom_llm_provider", "region_name", "cache_hit"}
    return {name: hidden[name] for name in allowed if name in hidden}


def _malformed(
    detail: str,
    deployment: HarborChatProviderConfig,
    logical_model: str,
    request_id: str,
) -> HarborChatProviderError:
    return HarborChatProviderError(
        f"malformed provider response: {detail}",
        operation="chat",
        provider=deployment.provider.value,
        logical_model=logical_model,
        provider_model=deployment.model,
        deployment=deployment.name,
        retryable=False,
        request_id=request_id,
    )
