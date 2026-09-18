"""Invocation-local trusted identity for legacy tool handler signatures."""

from contextvars import ContextVar

from harborrag_core.contracts.tools import ToolInvocationContext

current_invocation: ContextVar[ToolInvocationContext | None] = ContextVar(
    "harborrag_tool_invocation", default=None
)
