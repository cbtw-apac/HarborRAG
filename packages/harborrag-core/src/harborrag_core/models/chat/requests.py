from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from harborrag_core.base import StrictModel

from ..headers import ModelHeaderValue, protect_model_headers
from ..metadata import ModelRequestMetadata
from .messages import HarborChatMessage
from .tools import HarborChatTool


class HarborChatMetadata(ModelRequestMetadata):
    """Carry provider-neutral RAG context for a chat operation."""

    conversation_id: str | None = None
    retrieval_query: str | None = None
    document_ids: tuple[str, ...] = ()
    chunk_ids: tuple[str, ...] = ()
    reranker: str | None = None
    retrieval_latency_ms: float | None = None
    prompt_template_version: str | None = None
    source_citations: tuple[dict[str, Any], ...] = ()


class HarborTokenBudget(StrictModel):
    """Define prompt, completion, and total token limits for one request."""

    max_input_tokens: int | None = Field(default=None, gt=0)
    reserved_output_tokens: int = Field(default=0, ge=0)
    truncate_retrieved_context: bool = True
    fail_if_exceeded: bool = False

    @model_validator(mode="after")
    def validate_reserved_output(self) -> Self:
        """Require reserved completion capacity to leave a positive prompt budget."""
        if (
            self.max_input_tokens is not None
            and self.reserved_output_tokens >= self.max_input_tokens
        ):
            raise ValueError("reserved_output_tokens must be smaller than max_input_tokens")
        return self

    @property
    def effective_input_limit(self) -> int | None:
        """Return the prompt limit after reserving completion capacity."""
        if self.max_input_tokens is None:
            return None
        return self.max_input_tokens - self.reserved_output_tokens


class HarborChatRequest(StrictModel):
    """Represent a validated, provider-neutral chat request."""

    messages: tuple[HarborChatMessage, ...]
    logical_model: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    stop: str | tuple[str, ...] | None = None
    seed: int | None = None
    tools: tuple[HarborChatTool, ...] = ()
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: dict[str, Any] | type[BaseModel] | None = None
    reasoning_effort: str | None = None
    custom_headers: dict[str, ModelHeaderValue] = Field(default_factory=dict)
    metadata: HarborChatMetadata = Field(default_factory=HarborChatMetadata)
    token_budget: HarborTokenBudget | None = None
    cacheable: bool = False
    sensitive: bool = False
    extra_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("custom_headers", mode="before")
    @classmethod
    def protect_headers(cls, value: Any) -> Any:
        """Wrap credential-bearing header values before validation."""
        return protect_model_headers(value)

    @field_validator("messages", mode="before")
    @classmethod
    def normalize_messages(cls, value: Any) -> Any:
        """Coerce a list of messages into a tuple before validation."""
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("tools", mode="before")
    @classmethod
    def normalize_tools(cls, value: Any) -> Any:
        """Coerce a list of tools into a tuple before validation."""
        if isinstance(value, list):
            return tuple(value)
        return value
