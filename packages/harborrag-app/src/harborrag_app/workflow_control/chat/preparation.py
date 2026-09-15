"""Assemble the memory context, retrieval, and request for one chat turn.

Split out of ``service.py`` so the service keeps only the turn's control flow
(what is persisted, when, and what is recorded) while everything that decides
*what to ask the model* lives here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from harborrag_core.models.chat import HarborChatRequest
from harborrag_core.ports.conversation import ConversationHistoryRepository
from harborrag_core.ports.memory import MemoryIndex, MemoryRepository
from harborrag_core.ports.usage import ModelUsageRepository
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import MemoryContext
from harborrag_runtime.sdk import HarborRAG

from ..memory.context import recent_memory_context
from ..memory.identity import MemoryIdentity
from .evidence import ChatEvidence
from .options import ChatExecutionOptions
from .prompting import build_chat_request, history_messages
from .retrieval import relevant_results, search_documents

if TYPE_CHECKING:
    from harborrag_core.domain.retrieval import RetrievalResult

type RuntimeProvider = Callable[[], HarborRAG]


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
    """Load the last three exchanges, search the raw query, and build the request.

    Context is loaded before the current question is persisted, so the prompt
    contains that question once. Retrieval remains scoped to the caller's
    tenant and project; older conversation history does not enter the prompt.
    """

    context = await recent_memory_context(resources.memory, identity, query)
    response = await search_documents(
        resources.runtime(),
        context,
        identity=identity,
        settings=resources.settings,
        graph_search=options.graph_search,
    )
    # Gated once, before the results become both the model's evidence and the
    # caller's citations, so an answer is never reported as grounded in a
    # document that was too weak a match to be shown to the model.
    results = relevant_results(
        response.results,
        minimum=resources.settings.chat_retrieval_min_relevance,
    )
    graph_enabled = (
        resources.settings.chat_retrieval_graph_search
        if options.graph_search is None
        else options.graph_search
    )
    evidence = (
        ChatEvidence.prepare(
            replace(response, results=results),
            query=query,
            history=history_messages(context.messages),
            max_bytes=resources.settings.topology_retrieval_policy.max_context_tokens,
            overlay=graph_enabled,
        )
        if results
        else None
    )
    request = build_chat_request(
        query,
        identity=identity,
        results=results,
        context=context,
        model=options.model,
        evidence=evidence,
    )
    return PreparedTurn(request, evidence.passages if evidence is not None else results, context)


__all__ = ["ChatTurnResources", "PreparedTurn", "prepare_turn"]
