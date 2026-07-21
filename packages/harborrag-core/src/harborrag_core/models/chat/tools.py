from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from harborrag_core.base import ExtensibleModel, StrictModel


class HarborToolFunction(StrictModel):
    """Define a callable tool name, description, and JSON argument schema."""

    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    description: str | None = None
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    strict: bool | None = None


class HarborChatTool(StrictModel):
    """Wrap a function definition in HarborRAG's provider-neutral tool format."""

    type: Literal["function"] = "function"
    function: HarborToolFunction


class HarborToolCallFunction(ExtensibleModel):
    """Store the function name and serialized arguments returned by a model."""

    name: str
    arguments: str
    parsed_arguments: dict[str, Any] | None = None


class HarborToolCall(ExtensibleModel):
    """Represent one normalized tool invocation requested by a model."""

    id: str
    type: str = "function"
    function: HarborToolCallFunction
    index: int | None = None
