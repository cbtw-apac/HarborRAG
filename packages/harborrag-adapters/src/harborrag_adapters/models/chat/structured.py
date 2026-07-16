from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    HarborChatResponse,
    StructuredOutputDegradation,
)
from harborrag_core.models.errors import (
    HarborChatCapabilityError,
    HarborChatInvalidRequestError,
    HarborChatStructuredOutputError,
)
from pydantic import BaseModel, ValidationError

from .configs import HarborChatClientConfig, HarborChatProviderConfig
from .parameters import ChatMessageInput, prepare_chat_request

StructuredResponseT = TypeVar("StructuredResponseT", bound=BaseModel)


class StructuredChatClient(Protocol):
    """Define the minimal completion boundary used by structured execution."""

    def chat(self, *, request: HarborChatRequest) -> HarborChatResponse:
        """Generate one synchronous response."""
        ...

    async def achat(self, *, request: HarborChatRequest) -> HarborChatResponse:
        """Generate one asynchronous response."""
        ...


class StructuredOutputMode(StrEnum):
    """Identify the concrete LiteLLM structured-output strategy for one request."""

    NATIVE = "native"
    JSON = "json"
    PROMPT = "prompt"


@dataclass
class StructuredOutputAttemptState[ResponseT: BaseModel]:
    """Share validation and repair state between synchronous and asynchronous execution."""

    response_model: type[ResponseT]
    deployment: HarborChatProviderConfig
    request: HarborChatRequest
    schema: dict[str, Any]
    max_repair_attempts: int
    completion_attempts: int = 0

    def validate_or_prepare_repair(self, content: str | None) -> ResponseT | None:
        """Return validated data or update the request for the next bounded attempt."""

        self.completion_attempts += 1
        try:
            return validate_structured_content(content, self.response_model)
        except (TypeError, ValueError, ValidationError) as exc:
            if self.completion_attempts > self.max_repair_attempts:
                raise structured_validation_error(
                    exc,
                    response_model=self.response_model,
                    deployment=self.deployment,
                    request=self.request,
                    completion_attempts=self.completion_attempts,
                ) from exc
            self.request = build_repair_request(self.request, content, self.schema)
            return None


class StructuredOutputExecutor:
    """Execute typed chat responses with capability-aware bounded repair."""

    def __init__(self, client: StructuredChatClient, config: HarborChatClientConfig) -> None:
        """Store the client boundary and its validated policy configuration."""

        self._client = client
        self._config = config

    def chat(
        self,
        messages: Sequence[ChatMessageInput] | None,
        *,
        response_model: type[StructuredResponseT],
        request: HarborChatRequest | None,
        model: str | None,
        max_repair_attempts: int | None,
        request_kwargs: Mapping[str, Any],
    ) -> StructuredResponseT:
        """Generate a synchronous response and return only validated model data."""

        state = self._prepare(
            messages,
            response_model=response_model,
            request=request,
            model=model,
            max_repair_attempts=max_repair_attempts,
            request_kwargs=request_kwargs,
        )
        while True:
            response = self._client.chat(request=state.request)
            result = state.validate_or_prepare_repair(response.text)
            if result is not None:
                return result

    async def achat(
        self,
        messages: Sequence[ChatMessageInput] | None,
        *,
        response_model: type[StructuredResponseT],
        request: HarborChatRequest | None,
        model: str | None,
        max_repair_attempts: int | None,
        request_kwargs: Mapping[str, Any],
    ) -> StructuredResponseT:
        """Generate an asynchronous response and return only validated model data."""

        state = self._prepare(
            messages,
            response_model=response_model,
            request=request,
            model=model,
            max_repair_attempts=max_repair_attempts,
            request_kwargs=request_kwargs,
        )
        while True:
            response = await self._client.achat(request=state.request)
            result = state.validate_or_prepare_repair(response.text)
            if result is not None:
                return result

    def _prepare(
        self,
        messages: Sequence[ChatMessageInput] | None,
        *,
        response_model: type[StructuredResponseT],
        request: HarborChatRequest | None,
        model: str | None,
        max_repair_attempts: int | None,
        request_kwargs: Mapping[str, Any],
    ) -> StructuredOutputAttemptState[StructuredResponseT]:
        _validate_response_model(response_model)
        _validate_response_format_ownership(request, request_kwargs)
        _, deployment, prepared = prepare_chat_request(
            self._config,
            messages,
            request=request,
            model=model,
            request_kwargs=request_kwargs,
        )
        repairs = _repair_limit(self._config, max_repair_attempts)
        schema = _build_response_schema(response_model, deployment, prepared)
        mode = resolve_structured_output_mode(
            deployment,
            self._config.structured_output.degradation,
            request=prepared,
        )
        return StructuredOutputAttemptState(
            response_model=response_model,
            deployment=deployment,
            request=apply_structured_output_mode(prepared, response_model, schema, mode),
            schema=schema,
            max_repair_attempts=repairs,
        )


def resolve_structured_output_mode(
    deployment: HarborChatProviderConfig,
    degradation: StructuredOutputDegradation,
    *,
    request: HarborChatRequest,
) -> StructuredOutputMode:
    """Select native, JSON, or explicitly allowed prompt mode from capabilities."""

    capabilities = deployment.capabilities
    if capabilities.structured_output:
        return StructuredOutputMode.NATIVE
    if degradation is not StructuredOutputDegradation.REJECT and capabilities.json_mode:
        return StructuredOutputMode.JSON
    if degradation is StructuredOutputDegradation.PROMPT:
        return StructuredOutputMode.PROMPT
    raise HarborChatCapabilityError(
        "deployment cannot satisfy the configured structured-output policy",
        operation="chat",
        provider=deployment.provider.value,
        logical_model=request.logical_model,
        provider_model=deployment.model,
        deployment=deployment.name,
        request_id=request.metadata.request_id,
        retryable=False,
        metadata={"degradation": degradation.value},
    )


def apply_structured_output_mode(
    request: HarborChatRequest,
    response_model: type[BaseModel],
    schema: dict[str, Any],
    mode: StructuredOutputMode,
) -> HarborChatRequest:
    """Apply one normalized response format or schema prompt to a request."""

    if mode is StructuredOutputMode.NATIVE:
        response_format: dict[str, Any] = {
            "type": "json_schema",
            "json_schema": {
                "name": response_model.__name__,
                "schema": schema,
                "strict": True,
            },
        }
        return request.model_copy(update={"response_format": response_format})
    if mode is StructuredOutputMode.JSON:
        return request.model_copy(update={"response_format": {"type": "json_object"}})
    instruction = _schema_instruction(schema)
    messages = (HarborChatMessage.system(instruction), *request.messages)
    return request.model_copy(update={"messages": messages})


def validate_structured_content[ResponseT: BaseModel](
    content: str | None,
    response_model: type[ResponseT],
) -> ResponseT:
    """Parse and validate provider text as the requested Pydantic response model."""

    if content is None or not content.strip():
        raise ValueError("structured response content is empty")
    return response_model.model_validate_json(content)


def build_repair_request(
    request: HarborChatRequest,
    invalid_content: str | None,
    schema: dict[str, Any],
) -> HarborChatRequest:
    """Append one failed output and a constrained correction instruction."""

    previous = invalid_content if invalid_content is not None else "<empty response>"
    repair = (
        "The previous response failed JSON schema validation. Correct it and return only "
        f"one JSON object matching this schema: {_schema_json(schema)}"
    )
    messages = (
        *request.messages,
        HarborChatMessage.assistant(previous),
        HarborChatMessage.user(repair),
    )
    return request.model_copy(update={"messages": messages})


def structured_validation_error(
    error: Exception,
    *,
    response_model: type[BaseModel],
    deployment: HarborChatProviderConfig,
    request: HarborChatRequest,
    completion_attempts: int,
) -> HarborChatStructuredOutputError:
    """Build a sanitized terminal error for invalid or exhausted structured output."""

    return HarborChatStructuredOutputError(
        "structured response validation failed",
        operation="chat",
        provider=deployment.provider.value,
        logical_model=request.logical_model,
        provider_model=deployment.model,
        deployment=deployment.name,
        request_id=request.metadata.request_id,
        retryable=False,
        original_exception=error,
        metadata={
            "response_model": response_model.__name__,
            "completion_attempts": completion_attempts,
        },
    )


def _validate_response_model(response_model: object) -> None:
    if not isinstance(response_model, type) or not issubclass(response_model, BaseModel):
        raise HarborChatInvalidRequestError(
            "response_model must be a Pydantic BaseModel class",
            operation="chat",
            retryable=False,
        )


def _validate_response_format_ownership(
    request: HarborChatRequest | None,
    request_kwargs: Mapping[str, Any],
) -> None:
    if (request is not None and request.response_format is not None) or (
        "response_format" in request_kwargs
    ):
        raise HarborChatInvalidRequestError(
            "chat_structured owns response_format; do not supply it separately",
            operation="chat",
            retryable=False,
        )


def _repair_limit(config: HarborChatClientConfig, override: int | None) -> int:
    value = config.structured_output.max_repair_attempts if override is None else override
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
        raise HarborChatInvalidRequestError(
            "max_repair_attempts must be an integer from 0 through 3",
            operation="chat",
            retryable=False,
        )
    return value


def _build_response_schema(
    response_model: type[BaseModel],
    deployment: HarborChatProviderConfig,
    request: HarborChatRequest,
) -> dict[str, Any]:
    try:
        return response_model.model_json_schema()
    except Exception as exc:
        raise HarborChatStructuredOutputError(
            "response model cannot be represented as JSON Schema",
            operation="chat",
            provider=deployment.provider.value,
            logical_model=request.logical_model,
            provider_model=deployment.model,
            deployment=deployment.name,
            request_id=request.metadata.request_id,
            retryable=False,
            original_exception=exc,
            metadata={"response_model": response_model.__name__},
        ) from exc


def _schema_instruction(schema: dict[str, Any]) -> str:
    return (
        "Return only one valid JSON object matching this JSON Schema. Do not include markdown "
        f"or commentary: {_schema_json(schema)}"
    )


def _schema_json(schema: dict[str, Any]) -> str:
    return json.dumps(schema, separators=(",", ":"), sort_keys=True)
