"""The fixed three-exchange context shared by public chat and agent turns."""

from __future__ import annotations

import logging
from dataclasses import replace

from harborrag_core.ports.conversation import ConversationHistoryRepository
from harborrag_runtime.memory import MemoryContext, MemoryContextRequest

from .identity import MemoryIdentity

RECENT_EXCHANGES = 3
logger = logging.getLogger("harborrag.app.workflow_control.memory")


def memory_context_request(identity: MemoryIdentity, question: str) -> MemoryContextRequest:
    """Build identity for explicitly requested memory maintenance operations."""

    return MemoryContextRequest(
        tenant_id=identity.tenant_id,
        principal_id=identity.principal_id,
        user_id=identity.user_id,
        session_id=identity.session_id,
        question=question,
        project_id=identity.project_id,
    )


async def recent_memory_context(
    repository: ConversationHistoryRepository,
    identity: MemoryIdentity,
    question: str,
) -> MemoryContext:
    """Replay the latest three completed exchanges and preserve the raw query.

    Earlier messages remain stored for conversation browsing. Partial answers,
    unmatched questions, and tool messages are excluded from model context.
    This policy needs no summarization, query rewrite, or long-term recall.
    """

    context = empty_memory_context(question)
    try:
        messages = await repository.recent_complete_messages(
            identity.conversation(), limit=RECENT_EXCHANGES
        )
    except Exception:  # noqa: BLE001 - history availability must not fail a completion
        logger.warning(
            "Conversation context unavailable for tenant=%s user=%s session=%s",
            identity.tenant_id,
            identity.user_id,
            identity.session_id,
        )
        return context
    return replace(context, messages=messages)


def empty_memory_context(question: str) -> MemoryContext:
    """The no-history context a failed memory lookup degrades to."""

    return MemoryContext(
        messages=(),
        summary=None,
        recalled=(),
        standalone_query=question,
        rewritten=False,
        summary_written=False,
    )


__all__ = [
    "RECENT_EXCHANGES",
    "empty_memory_context",
    "memory_context_request",
    "recent_memory_context",
]
