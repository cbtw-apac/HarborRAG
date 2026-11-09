"""Assemble the memory context, retrieval, and request for one chat turn.

Split out of ``service.py`` so the service keeps only the turn's control flow
(what is persisted, when, and what is recorded) while everything that decides
*what to ask the model* lives here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from harborrag_core.models.chat import HarborChatRequest
from harborrag_core.ports.config_store import ConfigProvider
from harborrag_core.ports.conversation import ConversationHistoryRepository
from harborrag_core.ports.memory import MemoryIndex, MemoryRepository
from harborrag_core.ports.usage import ModelUsageRepository
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import MemoryContext
from harborrag_runtime.sdk import HarborRAG

from ..memory.context import empty_memory_context, recent_memory_context
from ..memory.identity import MemoryIdentity
from .evidence import ChatEvidence
from .options import ChatExecutionOptions
from .prompting import build_chat_request, history_messages
from .retrieval import chat_rerank, chat_top_k, relevant_results, search_documents

# ``ChatEvidence`` budgets the rendered prompt in bytes; the policy states it in
# tokens. Four is the usual conservative byte-per-token ratio for the mixed
# prose and JSON an evidence packet carries -- passing the token count straight
# through instead capped the whole evidence block at roughly a quarter of the
# context it was allowed, and rejected long questions outright.
_BYTES_PER_TOKEN = 4

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
    config: ConfigProvider | None = None

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
    *,
    filters: Mapping[str, object] | None = None,
) -> PreparedTurn:
    """Load the last three exchanges, search the raw query, and build the request.

    Context is loaded before the current question is persisted, so the prompt
    contains that question once; a one-off question has none. Retrieval stays in
    the caller's tenant and within ``filters``, at the ``retrieval.top_k`` and
    ``retrieval.rerank`` of the identity's project; older conversation history
    does not enter the prompt.
    """

    context = (
        await recent_memory_context(resources.memory, identity, query)
        if identity.session_id is not None
        else empty_memory_context(query)
    )
    graph_enabled = options.mode == "classic" and (
        resources.settings.chat_retrieval_graph_search
        if options.graph_search is None
        else options.graph_search
    )
    response = await search_documents(
        resources.runtime(),
        context,
        identity=identity,
        settings=resources.settings,
        observe_graph=graph_enabled,
        top_k=await chat_top_k(resources.config, identity, resources.settings),
        filters=filters if filters is not None else options.filters,
        rerank=await chat_rerank(resources.config, identity),
    )
    # Gated once, before the results become both the model's evidence and the
    # caller's citations, so an answer is never reported as grounded in a
    # document that was too weak a match to be shown to the model.
    results = relevant_results(
        response.results,
        minimum=resources.settings.chat_retrieval_min_relevance,
    )
    evidence = (
        ChatEvidence.prepare(
            replace(response, results=results),
            query=query,
            history=history_messages(context.messages),
            max_bytes=(
                resources.settings.topology_retrieval_policy.max_context_tokens * _BYTES_PER_TOKEN
            ),
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
        trace_id=options.trace_id,
    )
    return PreparedTurn(request, evidence.passages if evidence is not None else results, context)
