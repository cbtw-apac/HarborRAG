"""Rolling session summary: prompt the model, then shape the SESSION memory."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from harborrag_core.ports.conversation import ConversationMessage
from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryRepository,
    MemoryScope,
    MemoryType,
)

from .model import MemoryModelLike
from .prompting import generate_text, render_transcript
from .prompts import NO_SUMMARY_PLACEHOLDER, SUMMARY_SYSTEM_PROMPT, SUMMARY_USER_TEMPLATE

SUMMARY_IMPORTANCE = 0.6
"""Rolling summaries matter more than a raw turn, less than a stated fact."""

LAST_COVERED_KEY = "last_covered_message_id"
"""Metadata key recording how far into the session a summary reaches."""


def summary_memory_id(owner: MemoryOwner) -> str:
    """Return the deterministic id of one owner's rolling summary for one session.

    Keyed by tenant, end user, and session -- exactly the fields SESSION scope
    keys on -- so two users (or two tenants) whose session ids happen to
    collide never contend for one row. The key is hashed so the id stays a
    fixed, route-safe length. Pre-existing ``summary:{session}`` rows are
    still found (summary loading is an owner-scoped search, not an id
    lookup) and refreshed in place by their own owner.
    """

    resolved = recall_owner(owner)
    key = json.dumps(
        [resolved.tenant_id, resolved.user_id, resolved.session_id], separators=(",", ":")
    )
    return "summary:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def recall_owner(owner: MemoryOwner) -> MemoryOwner:
    """Fill ``user_id`` from ``principal_id`` so SESSION scope resolves.

    ``MemoryScope.SESSION`` keys on ``tenant_id``/``user_id``/``session_id``,
    so a summary written under an owner with no ``user_id`` would be invisible
    to its own writer. Saves and queries both go through this derivation, which
    keeps them symmetric.
    """

    if owner.user_id is not None:
        return owner
    return replace(owner, user_id=owner.principal_id)


async def summarize(
    model: MemoryModelLike,
    *,
    prior: str | None,
    messages: Sequence[ConversationMessage],
) -> str:
    """Fold ``prior`` and ``messages`` into one cumulative summary."""

    return await generate_text(
        model,
        system=SUMMARY_SYSTEM_PROMPT,
        user=SUMMARY_USER_TEMPLATE.format(
            prior_summary=prior or NO_SUMMARY_PLACEHOLDER,
            transcript=render_transcript(messages),
        ),
    )


@dataclass(frozen=True, slots=True)
class SummaryRecord:
    """Inputs for one rolling-summary write, kept keyword-only at the call site."""

    owner: MemoryOwner
    summary: str
    covered: tuple[ConversationMessage, ...]
    now: datetime
    memory_id: str | None = None

    def to_memory(self) -> Memory:
        """Build the SESSION/SUMMARY memory that replaces any prior summary.

        ``source_message_ids`` records only the messages *this* refresh folded
        in, so the row cannot grow without bound over a long session;
        ``metadata[LAST_COVERED_KEY]`` is the frontier the next turn reads.
        """

        session_id = self.owner.session_id or ""
        return Memory(
            memory_id=self.memory_id or summary_memory_id(self.owner),
            scope=MemoryScope.SESSION,
            memory_type=MemoryType.SUMMARY,
            owner=self.owner,
            content=self.summary,
            metadata={LAST_COVERED_KEY: self.covered[-1].message_id},
            importance=SUMMARY_IMPORTANCE,
            created_at=self.now,
            updated_at=self.now,
            valid_from=self.now,
            source_session_id=session_id or None,
            source_message_ids=tuple(message.message_id for message in self.covered),
        )


async def delete_session_summaries(memories: MemoryRepository, owner: MemoryOwner) -> int:
    """Delete every rolling summary of ``owner``'s session; return how many.

    Searches rather than deleting one id so a legacy ``summary:{session}``
    row goes too. The search is owner-scoped (SESSION visibility), so it can
    never reach another user's summary for a colliding session id.
    """

    caller = recall_owner(owner)
    if caller.session_id is None or caller.user_id is None:
        return 0
    found = await memories.search(
        MemoryQuery(
            owner=caller,
            scopes=(MemoryScope.SESSION,),
            memory_types=(MemoryType.SUMMARY,),
            limit=100,
            include_invalid=True,
        )
    )
    for memory in found:
        await memories.delete(caller, memory.memory_id)
    return len(found)


__all__ = [
    "LAST_COVERED_KEY",
    "SUMMARY_IMPORTANCE",
    "SummaryRecord",
    "delete_session_summaries",
    "recall_owner",
    "summarize",
    "summary_memory_id",
]
