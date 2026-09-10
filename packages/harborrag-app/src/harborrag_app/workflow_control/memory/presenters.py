"""Public projections of stored memories.

Provenance (which session and which messages a fact came from) is deliberately
part of the projection: a user asked "why does it think that?" deserves an
answer, and the same identifiers are what make the erasure endpoints
verifiable. Owner fields are never projected -- the caller already is the
owner, and echoing the isolation key back would invite callers to send it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from harborrag_core.ports.conversation import ConversationMessage, ConversationSummaryRow
    from harborrag_core.ports.memory import Memory


def _isoformat(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def memory_data(memory: Memory) -> dict[str, object]:
    """One stored memory as the API returns it."""

    return {
        "memory_id": memory.memory_id,
        "scope": memory.scope.value,
        "memory_type": memory.memory_type.value,
        "content": memory.content,
        "importance": memory.importance,
        "valid_from": _isoformat(memory.valid_from),
        "invalid_at": _isoformat(memory.invalid_at),
        "superseded_by": memory.superseded_by,
        "source_session_id": memory.source_session_id,
        "source_message_ids": list(memory.source_message_ids),
        "entity_ids": list(memory.entity_ids),
        "created_at": _isoformat(memory.created_at),
        "updated_at": _isoformat(memory.updated_at),
    }


def conversation_summary_data(row: ConversationSummaryRow) -> dict[str, object]:
    """One row of a conversation listing as the API returns it."""

    return {
        "session_id": row.session_id,
        "kind": row.kind,
        "title": row.title,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "message_count": row.message_count,
    }


def _citations(raw: str | None) -> list[object]:
    """Decode a stored citation payload, tolerating anything unparseable.

    The column is opaque text written by an earlier turn, so a row that
    predates the current shape (or was hand-edited) must read back as "no
    citations" rather than turning a history read into a 500.
    """

    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return list(parsed) if isinstance(parsed, list) else []


def conversation_message_data(message: ConversationMessage) -> dict[str, object]:
    """One stored conversation message as the API returns it.

    ``partial`` is projected because a reader must be able to tell a streamed
    answer that was cut short from a finished one.
    """

    return {
        "message_id": message.message_id,
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
        "token_count": message.token_count,
        "citations": _citations(message.citations_json),
        "run_id": message.run_id,
        "partial": message.partial,
    }


__all__ = ["conversation_message_data", "conversation_summary_data", "memory_data"]
