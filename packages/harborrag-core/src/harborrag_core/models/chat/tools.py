from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HarborToolFunction(BaseModel):
    """Define a callable tool name, description, and JSON argument schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    description: str | None = None
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    strict: bool | None = None


class HarborChatTool(BaseModel):
    """Wrap a function definition in HarborRAG's provider-neutral tool format."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["function"] = "function"
    function: HarborToolFunction


class HarborToolCallFunction(BaseModel):
    """Store the function name and serialized arguments returned by a model."""

    model_config = ConfigDict(extra="allow", frozen=True)
    name: str
    arguments: str
    parsed_arguments: dict[str, Any] | None = None


class HarborToolCall(BaseModel):
    """Represent one normalized tool invocation requested by a model."""

    model_config = ConfigDict(extra="allow", frozen=True)
    id: str
    type: str = "function"
    function: HarborToolCallFunction
    index: int | None = None
