"""Assemble one turn's conversation context from history plus memory."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from harborrag_core.base import utc_now
from harborrag_core.ports.conversation import (
    ConversationIdentity,
    ConversationMessage,
    ConversationMessageStore,
)
from harborrag_core.ports.memory import (
    Memory,
    MemoryEmbedder,
    MemoryIndex,
    MemoryOwner,
    MemoryQuery,
    MemoryRepository,
    MemoryScope,
    MemoryType,
)

from ..identity import conversation_identity
from .condenser import condense_question
from .entities import unique_ids
from .model import MemoryModelLike
from .policy import MemoryPolicy
from .recall import MemoryRecall
from .result import MemoryContext
from .summarizer import LAST_COVERED_KEY, SummaryRecord, recall_owner, summarize
from .tokens import approximate_tokens
from .trimming import TokenCounter, keep_boundary, total_tokens, trim_window

logger = logging.getLogger(__name__)

MAX_SUMMARY_CATCHUP_PAGES = 4
"""Bound history reads and summary model calls per build; later turns resume."""


@dataclass(frozen=True, slots=True)
class _WindowState:
    """The window and summary as they stand after the summarization step."""

    messages: tuple[ConversationMessage, ...]
    summary: str | None
    written: bool
    record: Memory | None = None


def _identity(owner: MemoryOwner) -> ConversationIdentity:
    return conversation_identity(owner, subject="per-turn memory context")


class MemoryContextBuilder:
    """Build the replay window, rolling summary, and standalone query for a turn.

    The question being answered must NOT be persisted yet when ``build`` is
    called: the window it returns is the history *before* this turn, and the
    caller appends the user message afterwards. A caller that appends first
    would see its own question replayed inside the window.

    Every model, repository, and index call degrades instead of failing the
    turn: a summarizer, condenser, or recall error is logged at WARNING and
    the turn continues with the untrimmed history, the original question, and
    no recalled memories.
    """

    def __init__(  # noqa: PLR0913 - one keyword-only collaborator per injected port
        self,
        *,
        policy: MemoryPolicy,
        messages: ConversationMessageStore,
        memories: MemoryRepository | None = None,
        model: MemoryModelLike | None = None,
        index: MemoryIndex | None = None,
        embedder: MemoryEmbedder | None = None,
        token_counter: TokenCounter | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy
        self._messages = messages
        self._memories = memories
        self._model = model
        self._count: TokenCounter = token_counter or approximate_tokens
        self._clock: Callable[[], datetime] = clock or utc_now
        self._recall = (
            MemoryRecall(
                policy=policy,
                memories=memories,
                index=index,
                embedder=embedder,
                clock=self._clock,
            )
            if memories is not None
            else None
        )

    async def build(
        self,
        owner: MemoryOwner,
        question: str,
        *,
        anchor_entity_ids: Sequence[str] = (),
    ) -> MemoryContext:
        """Return the memory context for ``question`` in ``owner``'s session.

        ``anchor_entity_ids`` are the knowledge-graph entities this turn's
        document retrieval surfaced; recall uses them only to re-rank, and the
        set it used is echoed back as ``MemoryContext.anchor_entity_ids``.

        Raises ``MemoryScopeError`` when ``owner`` lacks ``principal_id`` or
        ``session_id``; a conversation cannot be isolated without both.
        """

        identity = _identity(owner)
        anchors = unique_ids(tuple(anchor_entity_ids))
        limit = self._policy.recent_max_messages
        recent = await self._messages.recent_messages(identity, limit=limit)
        if not self._policy.enabled:
            return MemoryContext(
                messages=recent,
                summary=None,
                recalled=(),
                standalone_query=question,
                rewritten=False,
                summary_written=False,
            )
        stored = await self._load_summary(owner)
        state = await self._refresh_summary(owner, recent, stored)
        window = trim_window(
            state.messages,
            max_tokens=self._policy.recent_max_tokens,
            counter=self._count,
        )
        standalone, rewritten, wanted = await self._standalone(question, state.summary, window)
        recalled = await self._recalled(owner, standalone, anchors, wanted)
        return MemoryContext(
            messages=window,
            summary=state.summary,
            recalled=recalled,
            standalone_query=standalone,
            rewritten=rewritten,
            summary_written=state.written,
            anchor_entity_ids=anchors,
            wanted_types=wanted,
        )

    async def _recalled(
        self,
        owner: MemoryOwner,
        standalone: str,
        anchors: tuple[str, ...],
        wanted: tuple[MemoryType, ...],
    ) -> tuple[Memory, ...]:
        """Recall long-term memories for the resolved question, never failing.

        Recall runs against the standalone query rather than the raw question
        so a follow-up such as "and who owns it?" searches for what it
        actually means, and with the memory types that same rewrite reported
        the question wants.
        """

        if self._recall is None or self._policy.recall_top_k == 0:
            return ()
        try:
            return await self._recall.recall(
                owner, standalone, anchor_entity_ids=anchors, wanted_types=wanted
            )
        except Exception:
            logger.warning("long-term memory recall failed", exc_info=True)
            return ()

    async def _load_summary(self, owner: MemoryOwner) -> Memory | None:
        """Return the newest still-valid rolling summary for this session."""

        if self._memories is None:
            return None
        query = MemoryQuery(
            owner=recall_owner(owner),
            scopes=(MemoryScope.SESSION,),
            memory_types=(MemoryType.SUMMARY,),
            limit=1,
        )
        try:
            found = await self._memories.search(query)
        except Exception:
            logger.warning("loading the rolling session summary failed", exc_info=True)
            return None
        now = self._clock()
        valid = [memory for memory in found if memory.is_valid_at(now)]
        return max(valid, key=lambda memory: memory.updated_at) if valid else None

    async def _refresh_summary(
        self,
        owner: MemoryOwner,
        recent: Sequence[ConversationMessage],
        stored: Memory | None,
    ) -> _WindowState:
        """Catch up a summary whose frontier fell outside the recent window.

        Each catch-up call covers at most one history page, so its message
        count stays bounded even after an extended period without summaries.
        At most four pages are processed per build. A remaining backlog keeps
        the newest history window usable and resumes from the saved frontier
        on a later turn.
        """

        raw_frontier = stored.metadata.get(LAST_COVERED_KEY) if stored is not None else None
        frontier = raw_frontier if isinstance(raw_frontier, str) else None
        if (
            (stored is not None and frontier is None)
            or (stored is None and len(recent) < self._policy.recent_max_messages)
            or not recent
            or any(message.message_id == frontier for message in recent)
            or self._model is None
            or self._memories is None
        ):
            return await self._summarize(owner, recent, stored)
        written = False
        visited = {frontier}
        for _ in range(MAX_SUMMARY_CATCHUP_PAGES):
            try:
                pending = await self._messages.messages_after(
                    _identity(owner),
                    after_message_id=frontier,
                    limit=min(self._policy.recent_max_messages, 1000),
                )
            except Exception:
                logger.warning("loading unsummarized history failed", exc_info=True)
                return _WindowState(tuple(recent), stored.content if stored else None, written)
            if not pending:
                return _WindowState((), stored.content if stored else None, written)
            if pending[-1].message_id in visited:
                logger.warning("unsummarized history cursor did not advance")
                return _WindowState(tuple(recent), stored.content if stored else None, written)
            last = next(
                (
                    i
                    for i, message in enumerate(pending)
                    if message.message_id == recent[-1].message_id
                ),
                None,
            )
            if last is not None:
                state = await self._summarize(owner, pending[: last + 1], stored)
                return _WindowState(state.messages, state.summary, written or state.written)
            state = await self._summarize(owner, pending, stored, force=True)
            if state.record is None:
                return _WindowState(tuple(recent), state.summary, written)
            stored = state.record
            frontier = pending[-1].message_id
            visited.add(frontier)
            written = True
        return _WindowState(tuple(recent), stored.content if stored else None, written)

    async def _summarize(
        self,
        owner: MemoryOwner,
        recent: Sequence[ConversationMessage],
        stored: Memory | None,
        *,
        force: bool = False,
    ) -> _WindowState:
        """Refresh the rolling summary when the window outgrew its budget."""

        prior = stored.content if stored is not None else None
        if stored is not None:
            frontier = stored.metadata.get(LAST_COVERED_KEY)
            for index, message in enumerate(recent):
                if message.message_id == frontier:
                    recent = recent[index + 1 :]
                    break
        unchanged = _WindowState(messages=tuple(recent), summary=prior, written=False)
        if self._model is None or self._memories is None:
            return unchanged
        policy = self._policy
        budget = policy.summary_trigger_fraction * policy.recent_max_tokens
        if not force and total_tokens(recent, self._count) <= budget:
            return unchanged
        boundary = len(recent) if force else keep_boundary(recent, policy.summary_keep_messages)
        covered = tuple(recent[:boundary])
        if not covered:
            return unchanged
        try:
            summary = await summarize(self._model, prior=prior, messages=covered)
            if not summary:
                return unchanged
            record = SummaryRecord(
                # Session ownership excludes credential/project context. Keep
                # the original provenance when refreshing the existing row.
                owner=stored.owner if stored is not None else recall_owner(owner),
                summary=summary,
                covered=covered,
                now=self._clock(),
            ).to_memory()
            await self._memories.save(record)
        except Exception:
            logger.warning(
                "refreshing the rolling session summary failed; keeping the prior summary",
                exc_info=True,
            )
            return unchanged
        return _WindowState(
            messages=tuple(recent[boundary:]), summary=summary, written=True, record=record
        )

    async def _standalone(
        self,
        question: str,
        summary: str | None,
        window: Sequence[ConversationMessage],
    ) -> tuple[str, bool, tuple[MemoryType, ...]]:
        """Condense ``question`` against history, falling back to it verbatim.

        The rewrite also reports which memory types the question asks for.
        An unusable answer, a disabled rewrite, a missing model, a first turn,
        and any failure all hint nothing, leaving recall ranking exactly as it
        behaves without the type hint. A question the model handed back
        unchanged is not a rewrite, but its type judgement still stands.
        """

        unchanged: tuple[str, bool, tuple[MemoryType, ...]] = (question, False, ())
        if not self._policy.query_rewrite or self._model is None:
            return unchanged
        if not window and summary is None:
            return unchanged
        try:
            condensed = await condense_question(
                self._model,
                question=question,
                summary=summary,
                messages=window,
            )
        except Exception:
            logger.warning("condensing the standalone query failed", exc_info=True)
            return unchanged
        if condensed is None:
            return unchanged
        return condensed.query, condensed.query != question, condensed.wanted_types


__all__ = ["MemoryContextBuilder"]
