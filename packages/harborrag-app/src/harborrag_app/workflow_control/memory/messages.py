"""Build and persist per-message history records for one conversation turn.

The question and the answer are written separately, because they become
durable at different moments: the question is committed to as soon as the
turn starts, while the answer only exists once the provider has delivered
text. A streamed turn that dies mid-flight therefore still leaves both the
question and whatever answer reached the caller, the latter marked
``partial``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from uuid import uuid4

from harborrag_core.base import utc_now
from harborrag_core.ports.conversation import (
    ConversationIdentity,
    ConversationMessage,
    ConversationMessageStore,
)

logger = logging.getLogger("harborrag.app.workflow_control.memory")


def new_message_id() -> str:
    """Generate one opaque conversation-message identifier."""

    return f"msg-{uuid4().hex}"


def question_message(content: str, *, run_id: str | None = None) -> ConversationMessage:
    """The user's question, persisted before the model is called.

    Prompt tokens are not attributable to one message, so this record carries
    no ``token_count``; the answer carries the completion count instead.
    """

    return ConversationMessage(
        message_id=new_message_id(),
        role="user",
        content=content,
        created_at=utc_now(),
        run_id=run_id,
    )


def answer_message(
    content: str,
    *,
    citations: Sequence[object] = (),
    completion_tokens: int | None = None,
    run_id: str | None = None,
    partial: bool = False,
) -> ConversationMessage:
    """The assistant answer, with the citations already returned to the caller.

    Both the streaming and the non-streaming path build their answer here, so
    an incomplete stream is marked exactly the same way whichever path wrote
    it. ``partial`` defaults to False: a turn is complete unless a caller says
    otherwise, and it is a field on the canonical record so the flag survives
    a database round trip rather than only living in this process.
    """

    return ConversationMessage(
        message_id=new_message_id(),
        role="assistant",
        content=content,
        created_at=utc_now(),
        token_count=completion_tokens,
        citations_json=json.dumps(list(citations)) if citations else None,
        run_id=run_id,
        partial=partial,
    )


async def append_exchange(
    store: ConversationMessageStore,
    identity: ConversationIdentity,
    messages: Sequence[ConversationMessage],
) -> tuple[ConversationMessage, ...] | None:
    """Append ``messages`` and return them; ``None`` (never raise) on failure.

    The model call has already been paid for by the time this runs, so a
    memory failure must not turn a good answer into a 503. It is logged with
    identifiers only -- never prompt or answer text. The persisted records are
    returned so long-term extraction can cite them as provenance without
    re-deriving their identifiers.
    """

    persisted = tuple(messages)
    try:
        await store.append_messages(identity, persisted)
    except Exception:  # noqa: BLE001 - non-fatal by design
        logger.exception(
            "Conversation memory append failed for tenant=%s session=%s; "
            "answer returned without persisting the turn",
            identity.tenant_id,
            identity.session_id,
        )
        return None
    return persisted


async def append_message(
    store: ConversationMessageStore,
    identity: ConversationIdentity,
    message: ConversationMessage,
) -> ConversationMessage | None:
    """Append one message and return it; ``None`` (never raise) on failure."""

    persisted = await append_exchange(store, identity, (message,))
    return None if persisted is None else persisted[0]


__all__ = [
    "answer_message",
    "append_exchange",
    "append_message",
    "new_message_id",
    "question_message",
]
