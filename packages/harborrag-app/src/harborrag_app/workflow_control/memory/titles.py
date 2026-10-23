"""Generate one inexpensive conversation title without overwriting a manual edit."""

from __future__ import annotations

import logging

from harborrag_core.ports.conversation import ConversationHistoryRepository, ConversationIdentity

logger = logging.getLogger("harborrag.app.workflow_control.memory")


async def conversation_title(
    repository: ConversationHistoryRepository,
    identity: ConversationIdentity,
    *,
    prompt: str | None = None,
    run_id: str | None = None,
) -> str | None:
    """Optionally initialize the title, then return its current stored value.

    Callers supply a prompt only after the exchange has been persisted. The
    repository's compare-and-set gives manual edits precedence across workers.
    Title failures are advisory: they must never discard a completed answer.
    """

    try:
        if run_id is not None:
            messages = await repository.recent_messages(identity, limit=2)
            if (
                len(messages) != 2
                or messages[0].role != "user"
                or messages[-1].role != "assistant"
                or messages[-1].run_id != run_id
                or messages[-1].partial
            ):
                prompt = None
            else:
                prompt = messages[0].content
        if prompt is not None:
            title = " ".join(prompt.split()[:10])[:80].rstrip()
            if title:
                await repository.set_generated_title(identity, title=title)
        return await repository.get_title(identity)
    except Exception:  # noqa: BLE001 - a title is advisory metadata
        logger.warning("Conversation title unavailable for session=%s", identity.session_id)
        return None
