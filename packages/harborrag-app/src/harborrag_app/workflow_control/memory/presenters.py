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
    """One conversation as the explorer ``Session``; an untitled one has title ``""``."""

    return {
        "session_id": row.session_id,
        "title": row.title or "",
        "project_id": row.project_id,
        "profile_id": None,
        "message_count": row.message_count,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _citation(position: int, item: dict[str, object]) -> dict[str, object]:
    """One stored citation as the explorer ``Citation``.

    Answers written before the explorer contract stored ``document_title`` and
    no ``n``, ``snippet``, ``url`` or ``page``; they read back numbered by
    position with an empty snippet.
    """

    n = item.get("n")
    url = item.get("url")
    page = item.get("page")
    return {
        "n": n if isinstance(n, int) and not isinstance(n, bool) and n >= 1 else position,
        "chunk_id": str(item.get("chunk_id", "")),
        "document_id": str(item.get("document_id", "")),
        "title": str(item.get("title") or item.get("document_title") or ""),
        "snippet": str(item.get("snippet") or ""),
        "url": url if isinstance(url, str) else None,
        "page": page if isinstance(page, int) and not isinstance(page, bool) else None,
    }


def _citations(raw: str | None) -> list[dict[str, object]]:
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
    if not isinstance(parsed, list):
        return []
    items = [item for item in parsed if isinstance(item, dict)]
    return [_citation(position, item) for position, item in enumerate(items, start=1)]


def conversation_message_data(
    message: ConversationMessage, *, session_id: str
) -> dict[str, object]:
    """One stored message as the explorer ``Message``.

    Only an assistant answer carries ``mode``, ``citations``, ``trace_id`` and
    ``feedback``; a user message reads ``None``, ``[]``, ``None`` and ``None``.
    """

    answer = message.role == "assistant"
    return {
        "message_id": message.message_id,
        "session_id": session_id,
        "role": message.role,
        "content": message.content,
        "mode": message.mode if answer else None,
        "citations": _citations(message.citations_json) if answer else [],
        "trace_id": message.trace_id if answer else None,
        "feedback": message.feedback if answer else None,
        "created_at": message.created_at.isoformat(),
    }


__all__ = ["conversation_message_data", "conversation_summary_data", "memory_data"]
