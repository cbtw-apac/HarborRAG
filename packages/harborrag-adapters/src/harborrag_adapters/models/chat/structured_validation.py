from __future__ import annotations

from typing import Any

from pydantic import BaseModel

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
) -> HarborChatRequest:
    """Append one failed output and a constrained correction instruction."""

    previous = invalid_content if invalid_content is not None else "<empty response>"
    repair = (
        "The previous response failed JSON schema validation. Correct it and return only "
        f"one JSON object matching this schema: {schema_json(schema)}"
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
