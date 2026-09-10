"""Translate LangChain tool and structured-output requests into HarborRAG request fields."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from operator import itemgetter
from typing import Any, Literal

from langchain_core.output_parsers import (
    JsonOutputKeyToolsParser,
    JsonOutputParser,
    PydanticOutputParser,
    PydanticToolsParser,
)
from langchain_core.runnables import Runnable, RunnableMap, RunnablePassthrough
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel

from harborrag_core.models.chat import HarborChatTool

type ToolLike = dict[str, Any] | type | Callable[..., Any] | BaseTool | HarborChatTool
type StructuredOutputMethod = Literal["json_schema", "function_calling"]

_TOOL_CHOICE_ALIASES: dict[str, str] = {
    "auto": "auto",
    "none": "none",
    "required": "required",
    "any": "required",
}


def to_harbor_tools(
    tools: Sequence[ToolLike], *, strict: bool | None = None
) -> list[HarborChatTool]:
    """Convert LangChain tools, pydantic models, callables, or dicts into HarborRAG tools."""

    converted: list[HarborChatTool] = []
    for tool in tools:
        if isinstance(tool, HarborChatTool):
            converted.append(tool)
            continue
        definition = convert_to_openai_tool(tool, strict=strict)
        converted.append(HarborChatTool.model_validate(definition))
    return converted


def normalize_tool_choice(
    tool_choice: str | bool | dict[str, Any] | None,
    tools: Sequence[HarborChatTool],
) -> str | dict[str, Any] | None:
    """Map LangChain tool-choice spellings onto the provider-neutral request value."""

    if tool_choice is None or tool_choice is False:
        return None
    if tool_choice is True:
        return _single_tool_choice(tools) if len(tools) == 1 else "required"
    if isinstance(tool_choice, dict):
        return tool_choice
    alias = _TOOL_CHOICE_ALIASES.get(tool_choice)
    if alias is not None:
        return alias
    names = {tool.function.name for tool in tools}
    if tool_choice not in names:
        raise ValueError(f"tool_choice {tool_choice!r} does not name a bound tool")
    return {"type": "function", "function": {"name": tool_choice}}


def json_schema_response_format(schema: dict[str, Any] | type, *, strict: bool) -> dict[str, Any]:
    """Build the native ``json_schema`` response format HarborRAG's structured path emits."""

    if isinstance(schema, type) and issubclass(schema, BaseModel):
        json_schema = schema.model_json_schema()
        name = schema.__name__
    elif isinstance(schema, dict):
        json_schema = dict(schema)
        name = str(json_schema.get("title") or json_schema.get("name") or "response")
    else:
        raise TypeError("structured output schema must be a pydantic model class or a dict")
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "schema": json_schema, "strict": strict},
    }


def structured_output_parser(
    schema: dict[str, Any] | type, method: StructuredOutputMethod
) -> Runnable[Any, Any]:
    """Return the parser that turns model output into the requested structured value."""

    if isinstance(schema, type) and issubclass(schema, BaseModel):
        if method == "json_schema":
            return PydanticOutputParser(pydantic_object=schema)
        return PydanticToolsParser(tools=[schema], first_tool_only=True)
    if method == "json_schema":
        return JsonOutputParser()
    name = convert_to_openai_tool(schema)["function"]["name"]
    return JsonOutputKeyToolsParser(key_name=name, first_tool_only=True)


def compose_structured_output(
    model: Runnable[Any, Any],
    parser: Runnable[Any, Any],
    *,
    include_raw: bool,
) -> Runnable[Any, Any]:
    """Chain a bound model and parser, optionally exposing raw output and parse errors."""

    if not include_raw:
        return model | parser
    parser_assign = RunnablePassthrough.assign(
        parsed=itemgetter("raw") | parser, parsing_error=lambda _: None
    )
    parser_none = RunnablePassthrough.assign(parsed=lambda _: None)
    parser_with_fallback = parser_assign.with_fallbacks(
        [parser_none], exception_key="parsing_error"
    )
    return RunnableMap(raw=model) | parser_with_fallback


def _single_tool_choice(tools: Sequence[HarborChatTool]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": tools[0].function.name}}


__all__ = [
    "StructuredOutputMethod",
    "ToolLike",
    "compose_structured_output",
    "json_schema_response_format",
    "normalize_tool_choice",
    "structured_output_parser",
    "to_harbor_tools",
]
