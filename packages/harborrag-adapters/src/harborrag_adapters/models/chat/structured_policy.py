from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    StructuredOutputDegradation,
)
from harborrag_core.models.errors import HarborChatCapabilityError

from .configs import HarborChatProviderConfig
from .structured_strategy import StructuredOutputStrategy


class StructuredOutputMode(StrEnum):
    """Identify the concrete structured-output mode applied to one request."""

    NATIVE = "native"
    JSON = "json"
    PROMPT = "prompt"


def resolve_structured_output_mode(
    deployment: HarborChatProviderConfig,
    degradation: StructuredOutputDegradation,
    *,
    request: HarborChatRequest,
    strategy: StructuredOutputStrategy = StructuredOutputStrategy.AUTO,
) -> StructuredOutputMode:
    """Resolve an explicit or automatic strategy against deployment capabilities."""

    capabilities = deployment.capabilities
    if strategy is StructuredOutputStrategy.NATIVE_SCHEMA:
        if capabilities.structured_output:
            return StructuredOutputMode.NATIVE
        raise _capability_error(deployment, request, strategy)
    if strategy is StructuredOutputStrategy.JSON_MODE:
        if capabilities.json_mode:
            return StructuredOutputMode.JSON
        raise _capability_error(deployment, request, strategy)
    if strategy is StructuredOutputStrategy.PROMPT_FALLBACK:
        return StructuredOutputMode.PROMPT
    if capabilities.structured_output:
        return StructuredOutputMode.NATIVE
    if degradation is not StructuredOutputDegradation.REJECT and capabilities.json_mode:
        return StructuredOutputMode.JSON
    if degradation is StructuredOutputDegradation.PROMPT:
        return StructuredOutputMode.PROMPT
    raise _capability_error(deployment, request, strategy, degradation=degradation)


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
    messages = (HarborChatMessage.system(schema_instruction(schema)), *request.messages)
    return request.model_copy(update={"messages": messages})


def schema_instruction(schema: dict[str, Any]) -> str:
    """Return a bounded prompt instruction for prompt-degraded structured output."""

    return (
        "Return only one valid JSON object matching this JSON Schema. Do not include markdown "
        f"or commentary: {schema_json(schema)}"
    )


def schema_json(schema: dict[str, Any]) -> str:
    """Serialize a JSON schema deterministically for prompts and repairs."""

    return json.dumps(schema, separators=(",", ":"), sort_keys=True)


def _capability_error(
    deployment: HarborChatProviderConfig,
    request: HarborChatRequest,
    strategy: StructuredOutputStrategy,
    *,
    degradation: StructuredOutputDegradation | None = None,
) -> HarborChatCapabilityError:
    metadata = {"strategy": strategy.value}
    if degradation is not None:
        metadata["degradation"] = degradation.value
    return HarborChatCapabilityError(
        "deployment cannot satisfy the configured structured-output strategy",
        operation="chat",
        provider=deployment.provider.value,
        logical_model=request.logical_model,
        provider_model=deployment.model,
        deployment=deployment.name,
        request_id=request.metadata.request_id,
        retryable=False,
        metadata=metadata,
    )
