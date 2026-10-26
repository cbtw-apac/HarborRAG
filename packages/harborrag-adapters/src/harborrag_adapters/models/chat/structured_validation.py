from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic_core import ValidationError

from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest
from harborrag_core.models.errors import HarborChatStructuredOutputError

from .configs import HarborChatProviderConfig
from .structured_policy import schema_json


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
    *,
    error: Exception,
) -> HarborChatRequest:
    """Append one failed output and the bounded reason it needs correction."""

    previous = invalid_content if invalid_content is not None else "<empty response>"
    repair = (
        "The previous response failed structured output validation. "
        f"Validation error: {_repair_reason(error)}. Correct that error and return only "
        f"one JSON object matching this schema: {schema_json(schema)}"
    )
    messages = (
        *request.messages,
        HarborChatMessage.assistant(previous),
        HarborChatMessage.user(repair),
    )
    return request.model_copy(update={"messages": messages})


def _repair_reason(error: Exception) -> str:
    """Report custom Pydantic checks that JSON Schema cannot express."""

    if isinstance(error, ValidationError):
        messages = [
            str(item.get("msg", "invalid value"))
            for item in error.errors(include_input=False, include_url=False)[:3]
        ]
        return "; ".join(messages)[:400]
    return (str(error).splitlines() or [type(error).__name__])[0][:400]


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
