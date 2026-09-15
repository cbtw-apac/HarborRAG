"""Tenant-scoped document retrieval and evidence relevance filtering."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_runtime.sdk import RetrievalLane, RetrievalMode, RetrievalRequest

if TYPE_CHECKING:
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.contracts import RetrievalResponse
    from harborrag_runtime.memory import MemoryContext
    from harborrag_runtime.sdk import HarborRAG

    from ..memory.identity import MemoryIdentity


def relevant_results(
    results: Sequence[RetrievalResult],
    *,
    minimum: float,
) -> tuple[RetrievalResult, ...]:
    """Drop results the lane did not judge similar enough to be worth showing.

    Filtering here rather than in the prompt builder is deliberate: the same
    tuple becomes the model's evidence *and* the caller's citations, so one
    gate keeps an answer from being reported as grounded in a document it
    never used.

    A result whose lane reported no relevance is kept. ``None`` means "this
    lane cannot say", not "this is a poor match", and discarding on absence
    would silently empty the context for any backend that does not score.
    """

    if minimum <= 0.0:
        return tuple(results)
    return tuple(
        result for result in results if result.relevance is None or result.relevance >= minimum
    )


async def search_documents(
    runtime: HarborRAG,
    context: MemoryContext,
    *,
    identity: MemoryIdentity,
    settings: RuntimeSettings,
    graph_search: bool | None,
) -> RetrievalResponse:
    """Search the current question with the selected retrieval mode."""

    graph_enabled = settings.chat_retrieval_graph_search if graph_search is None else graph_search
    return await runtime.retrieval.search(
        RetrievalRequest(
            access=AccessContext(
                principal_id=identity.principal_id,
                tenant_id=TenantId(identity.tenant_id),
            ),
            query=context.standalone_query,
            top_k=settings.chat_retrieval_top_k,
            lane=RetrievalLane.HYBRID,
            observe_graph=graph_enabled,
            mode=RetrievalMode.LOCAL_SEMANTIC if graph_enabled else RetrievalMode.FLAT,
        )
    )


__all__ = ["relevant_results", "search_documents"]
