"""Transport-neutral options for one chat execution."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_runtime.chat import ChatPrompt


@dataclass(frozen=True, slots=True)
class ChatExecutionOptions:
    session_id: str
    system: ChatPrompt | None = None
    graph_search: bool | None = None


__all__ = ["ChatExecutionOptions"]
