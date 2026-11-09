"""Build and persist per-message history records for one conversation turn.

The question and the answer are written separately, because they become
durable at different moments: the question is committed to as soon as the
turn starts, while the answer only exists once the provider has delivered
text. A streamed turn that dies mid-flight therefore still leaves both the
question and whatever answer reached the caller, the latter marked
``partial``.

``record_exchange`` writes a completed exchange in one write instead, with the
answer's mode, trace and numbered citations, so the assistant message id can
serve as the completion's ``completion_id``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from harborrag_core.base import utc_now
from harborrag_core.ports.conversation import (
    ConversationIdentity,
    ConversationMessage,
    ConversationMessageStore,
    new_message_id,
)

logger = logging.getLogger("harborrag.app.workflow_control.memory")


@dataclass(frozen=True, slots=True)
class CitationRecord:
    """One numbered source of an assistant answer, stored as history returns it."""

    n: int
    chunk_id: str
    document_id: str
    title: str
    snippet: str
    url: str | None = None
    page: int | None = None


@dataclass(frozen=True, slots=True)
class ExchangeRecord:
    """One completed question and its answer, with what the answer was built from.

    ``answer_id`` is the assistant message id; a caller that must hand it out
    before the write (a completion's ``completion_id``) generates it with
    ``new_message_id`` and passes it here.
    """

    question: str
    answer: str
    mode: str
    trace_id: str
    citations: tuple[CitationRecord, ...] = ()
    completion_tokens: int | None = None
    question_id: str = field(default_factory=new_message_id)
    answer_id: str = field(default_factory=new_message_id)


async def record_exchange(
    store: ConversationMessageStore,
    identity: ConversationIdentity,
    exchange: ExchangeRecord,
) -> tuple[ConversationMessage, ConversationMessage]:
    """Persist a user message and its assistant answer in one write.

    Args:
        store: The conversation message store.
        identity: The owner and session the exchange belongs to.
        exchange: The question, the answer and the answer's metadata.

    Returns:
        The stored user message and assistant message, in that order.

    Raises:
        ValueError: The session does not exist in an in-memory store.
    """

    created_at = utc_now()
    question = ConversationMessage(
        message_id=exchange.question_id,
        role="user",
        content=exchange.question,
        created_at=created_at,
    )
    answer = ConversationMessage(
        message_id=exchange.answer_id,
        role="assistant",
        content=exchange.answer,
        created_at=created_at,
        token_count=exchange.completion_tokens,
        citations_json=json.dumps([asdict(citation) for citation in exchange.citations]),
        mode=exchange.mode,
        trace_id=exchange.trace_id,
    )
    await store.append_messages(identity, (question, answer))
    return question, answer


def question_message(content: str) -> ConversationMessage:
    """The user's question, persisted before the model is called.

    Prompt tokens are not attributable to one message, so this record carries
    no ``token_count``; the answer carries the completion count instead.
    """

    return ConversationMessage(
        message_id=new_message_id(),
        role="user",
        content=content,
        created_at=utc_now(),
    )


def answer_message(
    content: str,
    *,
    citations: Sequence[object] = (),
    completion_tokens: int | None = None,
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
    "CitationRecord",
    "ExchangeRecord",
    "answer_message",
    "append_exchange",
    "append_message",
    "new_message_id",
    "question_message",
    "record_exchange",
]
