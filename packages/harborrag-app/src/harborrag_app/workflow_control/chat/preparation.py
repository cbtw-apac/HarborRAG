"""Assemble the memory context, retrieval, and request for one chat turn.

Split out of ``service.py`` so the service keeps only the turn's control flow
(what is persisted, when, and what is recorded) while everything that decides
*what to ask the model* lives here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from harborrag_core.models.chat import HarborChatRequest
from harborrag_core.ports.conversation import ConversationHistoryRepository
from harborrag_core.ports.memory import MemoryIndex, MemoryRepository
from harborrag_core.ports.usage import ModelUsageRepository
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import MemoryContext
from harborrag_runtime.sdk import HarborRAG

from ..memory.context import empty_memory_context, memory_context_request
from ..memory.identity import MemoryIdentity
from .options import ChatExecutionOptions
from .prompting import build_chat_request
from .retrieval import graph_anchor_ids, relevant_results, search_documents

if TYPE_CHECKING:
    from harborrag_core.domain.retrieval import RetrievalResult

type RuntimeProvider = Callable[[], HarborRAG]

logger = logging.getLogger("harborrag.app.workflow_control.chat")


@dataclass(frozen=True, slots=True)
class ChatTurnResources:
    """Every port one chat turn touches, bundled so helpers stay small.

    ``usage`` is the durable per-request accounting ledger; like the long-term
    memory ports it is optional, and a deployment without one still answers.
    """

    runtime_provider: RuntimeProvider
    settings: RuntimeSettings
    memory: ConversationHistoryRepository
    memories: MemoryRepository | None = None
    index: MemoryIndex | None = None
    usage: ModelUsageRepository | None = None

    def runtime(self) -> HarborRAG:
        return self.runtime_provider()


@dataclass(frozen=True, slots=True)
class PreparedTurn:
    """Everything a completion needs once the session and project are known."""

    request: HarborChatRequest
    results: tuple[RetrievalResult, ...]
    context: MemoryContext


async def prepare_turn(
    resources: ChatTurnResources,
    query: str,
    identity: MemoryIdentity,
    options: ChatExecutionOptions,
) -> PreparedTurn:
    """Assemble the context, search, then re-rank recall on what search found.

    The order is load-bearing twice over. Context comes first because it
    produces the standalone query retrieval must search for; retrieval then
    reports which graph nodes its results sit on, which is better evidence of
    what the turn is about than the question alone, so recall alone is re-run
    against them. The standalone query is reused, so the rewrite and the
    rolling summary are paid for exactly once.

    It also comes before the caller persists the question. ``MemoryContextBuilder``
    reads the recent window and must not see the turn it is answering; a
    caller that appended first would replay its own question and defeat the
    first-turn no-rewrite rule.
    """

    context = await _context(resources, identity, query)
    response = await search_documents(
        resources.runtime(),
        context,
        identity=identity,
        settings=resources.settings,
        graph_search=options.graph_search,
    )
    context = await _anchored(
        resources,
        context,
        identity,
        query,
        anchors=graph_anchor_ids(response.diagnostics),
    )
    # Gated once, before the results become both the model's evidence and the
    # caller's citations, so an answer is never reported as grounded in a
    # document that was too weak a match to be shown to the model.
    results = relevant_results(
        response.results,
        minimum=resources.settings.chat_retrieval_min_relevance,
    )
    request = build_chat_request(
        query,
        identity=identity,
        results=results,
        context=context,
        model=options.model,
    )
    return PreparedTurn(request, results, context)


async def _anchored(  # noqa: PLR0913 - one bundle plus the anchoring inputs
    resources: ChatTurnResources,
    context: MemoryContext,
    identity: MemoryIdentity,
    query: str,
    *,
    anchors: Sequence[str],
) -> MemoryContext:
    """Re-rank recall on the graph nodes retrieval surfaced, or keep it as is.

    Skipped without a memory store, without anchors, or when recall is off;
    the unanchored context is kept on any failure, because a re-ranking that
    did not happen beats a turn that did not answer.
    """

    if not anchors or resources.memories is None:
        return context
    try:
        return await resources.runtime().memory.recall_anchored(
            memory_context_request(identity, query),
            context,
            memories=resources.memories,
            index=resources.index,
            anchor_entity_ids=anchors,
        )
    except Exception:  # noqa: BLE001 - non-fatal by design
        logger.warning(
            "Anchoring conversation memory recall failed for tenant=%s session=%s; "
            "answering from the unanchored context",
            identity.tenant_id,
            identity.session_id,
            exc_info=True,
        )
        return context


async def _context(
    resources: ChatTurnResources,
    identity: MemoryIdentity,
    query: str,
) -> MemoryContext:
    """Apply the memory policy; degrade to no history rather than fail the turn.

    History is context, not the answer. If the store or the memory model is
    unavailable the question is still answerable from retrieval alone, so a
    failure here is logged with identifiers only and the turn continues
    against the raw query.
    """

    try:
        return await resources.runtime().memory.build_context(
            memory_context_request(identity, query),
            messages=resources.memory,
            memories=resources.memories,
            index=resources.index,
        )
    except Exception:  # noqa: BLE001 - non-fatal by design
        logger.exception(
            "Conversation memory context failed for tenant=%s session=%s; "
            "answering from retrieval alone",
            identity.tenant_id,
            identity.session_id,
        )
        return empty_memory_context(query)


__all__ = ["ChatTurnResources", "PreparedTurn", "prepare_turn"]
