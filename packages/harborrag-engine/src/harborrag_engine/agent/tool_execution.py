"""Bounded tool-call execution helpers: per-turn caps and result truncation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any

from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatTool,
    HarborToolCall,
    HarborToolFunction,
)
from harborrag_core.ports.agent_runs import AgentToolExecution

from .guard import digest_arguments
from .protocols import AgentToolSpec

ADVANCED_VECTOR_TOOL = "vector_search_advanced"
# A provider adapter bug or a model prompted by untrusted retrieved content
# could return an unbounded number of tool calls in one response; without a
# cap, asyncio.gather below would fire that many concurrent tool executions
# despite the loop's per-turn step budget. Every call still gets a reply
# (never silently dropped) -- calls past the cap are rejected rather than
# executed, since providers require a tool-role reply for every tool_call_id
# they issued.
MAX_TOOL_CALLS_PER_TURN = 8
# Tool output is appended to conversation history and re-sent to the model on
# every subsequent turn. Without a cap, a single oversized result (e.g. a
# tool that leaks megabytes of retrieved text) grows unboundedly across
# turns; 16,000 characters matches the bound EvidenceBuilder already applies
# to retrieved text elsewhere in this package.
MAX_TOOL_RESULT_CHARS = 16_000
_MAX_TOOL_RESULT_DEPTH = 8
_MAX_TOOL_RESULT_ITEMS = 128


def rejected_execution(
    call: HarborToolCall, *, step: int, error: str
) -> tuple[HarborChatMessage, AgentToolExecution]:
    """Reply to a tool call that will never execute, without calling the tool."""
    result = {"ok": False, "error": error}
    content = bounded_tool_result_content(result)
    arguments = call.function.parsed_arguments
    digest = digest_arguments(
        arguments if isinstance(arguments, dict) else {"__unparsed__": call.function.arguments}
    )
    return (
        HarborChatMessage.tool(content, tool_call_id=call.id, name=call.function.name),
        AgentToolExecution(
            step=step,
            call_id=call.id,
            tool=call.function.name,
            ok=False,
            arguments_digest=digest,
        ),
    )


def bounded_tool_result_content(result: dict[str, object]) -> str:
    remaining = [MAX_TOOL_RESULT_CHARS]
    normalized = _bounded_json_value(result, remaining=remaining, seen=set(), depth=0)
    content = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )
    if len(content) <= MAX_TOOL_RESULT_CHARS:
        return content
    omitted = len(content) - MAX_TOOL_RESULT_CHARS
    return f"{content[:MAX_TOOL_RESULT_CHARS]}...<truncated {omitted} chars>"


def _bounded_json_value(
    value: Any,
    *,
    remaining: list[int],
    seen: set[int],
    depth: int,
) -> object:
    """Normalize an arbitrary provider result without traversing it unboundedly."""

    scalar = _bounded_scalar(value, remaining)
    if scalar is not _NOT_SCALAR:
        return scalar
    if depth >= _MAX_TOOL_RESULT_DEPTH:
        return "<maximum depth exceeded>"

    identity = id(value)
    if identity in seen:
        return "<circular reference>"

    if isinstance(value, Mapping):
        return _bounded_mapping(value, remaining=remaining, seen=seen, depth=depth)

    if isinstance(value, Sequence):
        return _bounded_sequence(value, remaining=remaining, seen=seen, depth=depth)

    return _bounded_json_value(
        str(value),
        remaining=remaining,
        seen=seen,
        depth=depth,
    )


_NOT_SCALAR = object()


def _bounded_scalar(value: Any, remaining: list[int]) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if value.bit_length() <= 4096 else "<integer exceeds limit>"
    if isinstance(value, float):
        return value if isfinite(value) else "<non-finite number>"
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return _NOT_SCALAR
    limit = max(0, remaining[0])
    text = value[:limit]
    remaining[0] -= len(text)
    return text if len(text) == len(value) else f"{text}...<truncated>"


def _bounded_mapping(
    value: Mapping[object, object],
    *,
    remaining: list[int],
    seen: set[int],
    depth: int,
) -> dict[str, object]:
    seen.add(id(value))
    try:
        result: dict[str, object] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_TOOL_RESULT_ITEMS or remaining[0] <= 0:
                result["<truncated>"] = "collection limit exceeded"
                break
            normalized_key = str(key)[:256]
            remaining[0] -= len(normalized_key)
            result[normalized_key] = _bounded_json_value(
                item, remaining=remaining, seen=seen, depth=depth + 1
            )
        return result
    finally:
        seen.remove(id(value))


def _bounded_sequence(
    value: Sequence[object],
    *,
    remaining: list[int],
    seen: set[int],
    depth: int,
) -> list[object]:
    seen.add(id(value))
    try:
        result: list[object] = []
        for index, item in enumerate(value):
            if index >= _MAX_TOOL_RESULT_ITEMS or remaining[0] <= 0:
                result.append("<collection truncated>")
                break
            result.append(
                _bounded_json_value(item, remaining=remaining, seen=seen, depth=depth + 1)
            )
        return result
    finally:
        seen.remove(id(value))


def tool_definition(spec: AgentToolSpec, graph_search: bool) -> HarborChatTool:
    schema = json.loads(json.dumps(spec.input_schema))
    if spec.name == ADVANCED_VECTOR_TOOL and not graph_search:
        properties = schema.get("properties")
        if isinstance(properties, dict):
            properties.pop("observe_graph", None)
    return HarborChatTool(
        function=HarborToolFunction(
            name=spec.name,
            description=spec.description,
            parameters=schema,
        )
    )


__all__ = [
    "ADVANCED_VECTOR_TOOL",
    "MAX_TOOL_CALLS_PER_TURN",
    "MAX_TOOL_RESULT_CHARS",
    "bounded_tool_result_content",
    "rejected_execution",
    "tool_definition",
]
