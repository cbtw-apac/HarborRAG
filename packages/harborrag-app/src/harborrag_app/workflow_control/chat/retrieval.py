"""Document retrieval for one chat turn, and the graph anchors it surfaces.

Split out of ``service.py`` so that module stays about orchestrating a turn.
Two things live here: issuing the search the assembled memory context asked
for -- seeded with the graph entities the caller's own memories reference --
and reading back out of the response which graph nodes the results actually
sat on, so long-term recall can be re-ranked against them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_runtime.memory import recalled_entity_ids
from harborrag_runtime.sdk import RetrievalLane, RetrievalRequest

if TYPE_CHECKING:
    from harborrag_runtime.config.settings import RuntimeSettings
    from harborrag_runtime.contracts import RetrievalResponse
    from harborrag_runtime.memory import MemoryContext
    from harborrag_runtime.sdk import HarborRAG

    from ..memory.identity import MemoryIdentity

# Anchors are a set the ranker intersects against, so a stray id costs nothing
# but a long walk over a large diagnostics payload does. Ten observed results
# times a handful of structural nodes each is the shape this bounds.
MAX_ANCHORS = 50


def graph_anchor_ids(diagnostics: Mapping[str, object]) -> tuple[str, ...]:
    """Graph node keys the observed retrieval results sat on, in result order.

    Read out of the ``graph_documents`` provenance the retrieval response
    already carries when graph observation ran -- the chunk, section, and
    document-version nodes each result reached. Every node kind is kept
    rather than guessing which are "entities": anchoring is a set overlap
    against ``Memory.entity_ids``, so an id no memory references simply never
    matches, while filtering by kind risks discarding the ones that would.

    Empty -- never an error -- when observation was off or the payload does
    not have the expected shape; anchoring is an improvement on recall, so a
    diagnostics shape it does not recognise must degrade silently.
    """

    keys: dict[str, None] = {}
    for document in _items(diagnostics.get("graph_documents")):
        for neighborhood in _items(document.get("related_results")):
            for node in _items(neighborhood.get("nodes")):
                key = node.get("node_key")
                if isinstance(key, str) and key.strip():
                    keys.setdefault(key.strip())
                if len(keys) >= MAX_ANCHORS:
                    return tuple(keys)
    return tuple(keys)


def _items(value: object) -> tuple[Mapping[str, object], ...]:
    """The mappings inside ``value``, or ``()`` for anything else."""

    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


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
    """Search for the standalone query, seeded by what memory already knows.

    The seeds are the graph entities this session's recalled memories
    reference, so traversal can start from ground the user's own history
    established rather than only from what the vector lanes happened to hit.
    They widen graph observation only, so they are inert unless observation is
    on for this turn.
    """

    return await runtime.retrieval.search(
        RetrievalRequest(
            access=AccessContext(
                principal_id=identity.principal_id,
                tenant_id=TenantId(identity.tenant_id),
            ),
            query=context.standalone_query,
            top_k=settings.chat_retrieval_top_k,
            lane=RetrievalLane.HYBRID,
            observe_graph=(
                settings.chat_retrieval_graph_search if graph_search is None else graph_search
            ),
            graph_seed_node_keys=recalled_entity_ids(context),
        )
    )


__all__ = ["MAX_ANCHORS", "graph_anchor_ids", "relevant_results", "search_documents"]
