"""The immutable result of assembling one turn's conversation context."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.ports.conversation import ConversationMessage
from harborrag_core.ports.memory import Memory, MemoryType


@dataclass(frozen=True, slots=True)
class MemoryContext:
    """Everything one turn needs from memory, already budgeted.

    ``messages`` is the verbatim window to replay (oldest-first) and excludes
    the question being answered; ``summary`` covers the older turns the window
    dropped. ``recalled`` is the long-term memories recall selected for the
    standalone query, already ranked, deduplicated, and trimmed to their share
    of the prompt. ``standalone_query`` is what retrieval should search for --
    ``rewritten`` says whether the model changed it, and ``summary_written``
    whether assembling this context persisted a refreshed summary.

    ``wanted_types`` is the memory types the rewrite reported this question
    asks for, echoed back for observability; recall used them only to
    re-rank. It is empty whenever no rewrite happened.
    """

    messages: tuple[ConversationMessage, ...]
    summary: str | None
    recalled: tuple[Memory, ...]
    standalone_query: str
    rewritten: bool
    summary_written: bool
    anchor_entity_ids: tuple[str, ...] = ()
    wanted_types: tuple[MemoryType, ...] = ()


__all__ = ["MemoryContext"]
