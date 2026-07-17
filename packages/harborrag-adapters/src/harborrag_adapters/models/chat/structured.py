from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from harborrag_core.models.chat import (
    HarborChatRequest,
    HarborChatResponse,
)
from harborrag_core.models.errors import (
    HarborChatInvalidRequestError,
    HarborChatStructuredOutputError,
)
from pydantic import BaseModel, ValidationError

from .configs import HarborChatClientConfig, HarborChatProviderConfig
from .parameters import ChatMessageInput, prepare_chat_request
from .structured_policy import (
    apply_structured_output_mode,
    resolve_structured_output_mode,
)
from .structured_strategy import StructuredOutputStrategy
from .structured_validation import (
    build_repair_request,
    structured_validation_error,
    validate_structured_content,
)

StructuredResponseT = TypeVar("StructuredResponseT", bound=BaseModel)


class StructuredChatClient(Protocol):
    """Define the minimal completion boundary used by structured execution."""

    def chat(self, *, request: HarborChatRequest) -> HarborChatResponse:
        """Generate one synchronous response."""
        ...

    async def achat(self, *, request: HarborChatRequest) -> HarborChatResponse:
        """Generate one asynchronous response."""
        ...


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
        strategy: StructuredOutputStrategy | None,
        request_kwargs: Mapping[str, Any],
    ) -> StructuredResponseT:
        """Generate a synchronous response and return only validated model data."""

        state = self._prepare(
            messages,
            response_model=response_model,
            request=request,
            model=model,
            max_repair_attempts=max_repair_attempts,
            strategy=strategy,
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
        strategy: StructuredOutputStrategy | None,
        request_kwargs: Mapping[str, Any],
    ) -> StructuredResponseT:
        """Generate an asynchronous response and return only validated model data."""

        state = self._prepare(
            messages,
            response_model=response_model,
            request=request,
            model=model,
            max_repair_attempts=max_repair_attempts,
            strategy=strategy,
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
        strategy: StructuredOutputStrategy | None,
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
            strategy=strategy or self._config.structured_output.strategy,
        )
        return StructuredOutputAttemptState(
            response_model=response_model,
            deployment=deployment,
            request=apply_structured_output_mode(prepared, response_model, schema, mode),
            schema=schema,
            max_repair_attempts=repairs,
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
