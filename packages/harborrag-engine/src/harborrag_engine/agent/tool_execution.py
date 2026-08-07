"""Bounded tool-call execution helpers: per-turn caps and result truncation."""

from __future__ import annotations

import json

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
    content = json.dumps(result, sort_keys=True, separators=(",", ":"), default=str)
    if len(content) <= MAX_TOOL_RESULT_CHARS:
        return content
    omitted = len(content) - MAX_TOOL_RESULT_CHARS
    return f"{content[:MAX_TOOL_RESULT_CHARS]}...<truncated {omitted} chars>"


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
