"""Rolling session summary: prompt the model, then shape the SESSION memory."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256

from harborrag_core.ports.conversation import ConversationMessage
from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryScope, MemoryType

from .model import MemoryModelLike
from .prompting import generate_text, render_transcript
from .prompts import NO_SUMMARY_PLACEHOLDER, SUMMARY_SYSTEM_PROMPT, SUMMARY_USER_TEMPLATE

SUMMARY_IMPORTANCE = 0.6
"""Rolling summaries matter more than a raw turn, less than a stated fact."""

LAST_COVERED_KEY = "last_covered_message_id"
"""Metadata key recording how far into the session a summary reaches."""


def summary_memory_id(owner: MemoryOwner) -> str:
    """Return the deterministic id of a conversation's single rolling summary.

    ``memories.memory_id`` is a global primary key, so the id must carry the
    whole SESSION identity -- tenant, user, and session -- or two users (or
    tenants) reusing a session id such as ``"default"`` would fight over one
    row. The identity is hashed so a 128-character session id still fits the
    column. ``principal_id`` is deliberately left out: SESSION scope does not
    key on it, so two credentials of one user share the history and therefore
    the summary of it.
    """

    resolved = recall_owner(owner)
    identity = json.dumps([resolved.tenant_id, resolved.user_id, resolved.session_id])
    return f"summary:{sha256(identity.encode()).hexdigest()}"


def legacy_summary_memory_id(session_id: str) -> str:
    """Return the id releases before the hashed one gave a session's summary.

    Only ever looked up so the builder can carry such a row forward under
    ``summary_memory_id`` and delete it; nothing writes this id any more.
    """

    return f"summary:{session_id}"


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
    expires_at: datetime | None = None

    def to_memory(self) -> Memory:
        """Build the SESSION/SUMMARY memory that replaces any prior summary.

        ``source_message_ids`` records only the messages *this* refresh folded
        in, so the row cannot grow without bound over a long session;
        ``metadata[LAST_COVERED_KEY]`` is the frontier the next turn reads.
        Every refresh rewrites ``expires_at``, so retention counts from the
        session's last summarized activity, not from its first summary.
        """

        session_id = self.owner.session_id or ""
        return Memory(
            memory_id=summary_memory_id(self.owner),
            scope=MemoryScope.SESSION,
            memory_type=MemoryType.SUMMARY,
            owner=self.owner,
            content=self.summary,
            metadata={LAST_COVERED_KEY: self.covered[-1].message_id},
            importance=SUMMARY_IMPORTANCE,
            created_at=self.now,
            updated_at=self.now,
            expires_at=self.expires_at,
            valid_from=self.now,
            source_session_id=session_id or None,
            source_message_ids=tuple(message.message_id for message in self.covered),
        )


__all__ = [
    "LAST_COVERED_KEY",
    "SUMMARY_IMPORTANCE",
    "SummaryRecord",
    "legacy_summary_memory_id",
    "recall_owner",
    "summarize",
    "summary_memory_id",
]
