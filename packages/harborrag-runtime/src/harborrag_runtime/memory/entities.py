"""Resolve conversation-memory entity mentions against the knowledge graph.

Extraction proposes surface forms ("the Atlas migration"); ranking needs
curated ids. This adapter closes that gap using the narrowest read path the
graph already exposes -- ``expand_subgraph``'s start-node resolution, which
matches a selector against ``node_key``, ``logical_id``, or a lowercased exact
``title`` inside one tenant -- so no new Cypher is added and the graph is never
written to. Conversation memory is only ever *linked* to the document graph by
id.

Every failure is degradation: a missing graph, an unreachable database, or an
unresolvable mention yields no anchor, logged at WARNING, and recall falls back
to relevance, recency, and importance alone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Protocol

from harborrag_core.ports.memory import ResolvedEntity
from harborrag_core.retrieval import GraphDirection, GraphSubgraphQuery
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext

if TYPE_CHECKING:
    from harborrag_core.ingestion import GraphNodeRecord
    from harborrag_core.ports.memory import MemoryEntityResolver
    from harborrag_engine.retrieval import AuthoritativeSubgraphResult
    from harborrag_runtime.config.settings import RuntimeSettings

logger = logging.getLogger("harborrag.runtime.memory")

# One resolver call is a side errand on the extraction path, not a search. The
# cap bounds the fan-out of an LLM that proposed a hundred mentions; anything
# past it is dropped rather than queued.
MAX_MENTIONS = 16

# The start node is identified by *matching it against the mention*, never by
# position: the engine widens the node budget before the adapter call and then
# keeps the first `max_nodes` **active** nodes, so an inactive start node would
# otherwise hand back one of its neighbours. Asking for a few and matching
# turns that into a miss instead of a wrong answer.
_LOOKUP_MAX_NODES = 4
_LOOKUP_MAX_DEPTH = 1

# Confidence is the exactness of the match the graph's own start resolution
# made, because a node lookup returns no score to defend one from:
#   1.00  the mention *is* the node's identifier (``node_key`` or ``logical_id``),
#         so the caller already held a curated id;
#   0.90  the node's ``title`` equals the mention exactly;
#   0.75  the node's ``title`` equals it only case-insensitively -- which is
#         precisely what the underlying ``toLower(title) = toLower($selector)``
#         predicate matched on, so the surface form was never confirmed.
# A returned node matching none of these is not the mention's node at all and
# is dropped, not scored.
_IDENTIFIER_CONFIDENCE = 1.0
_EXACT_TITLE_CONFIDENCE = 0.9
_FOLDED_TITLE_CONFIDENCE = 0.75

DEFAULT_MIN_CONFIDENCE = 0.5
"""Floor below which a match is not worth anchoring on.

Deliberately the resolver's own knob rather than ``MemoryPolicy``'s
``entity_confidence_floor``: the policy floor is applied by extraction to
whatever a resolver returns, so a resolver enforcing its own floor cannot
weaken it.
"""

# Resolution reads one tenant's graph on memory's behalf, never a human's.
_RESOLVER_PRINCIPAL = "memory-entity-resolver"

_UNAVAILABLE = "memory entity mentions stay unresolved"


class GraphEntityLookup(Protocol):
    """The slice of runtime retrieval an entity resolver needs.

    Narrower than ``RuntimeRetrievalService`` on purpose: resolution is a
    read of one node, so it must not be able to reach the write side or the
    vector lanes even by accident.
    """

    @property
    def graph_retrieval_available(self) -> bool: ...

    async def search_graph_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        access: AccessContext,
    ) -> AuthoritativeSubgraphResult: ...


type GraphLookupProvider = Callable[[], Awaitable[GraphEntityLookup]]
"""Deferred access to retrieval, so no graph client is opened until first use."""


def _dedupe(mentions: Sequence[str]) -> tuple[str, ...]:
    """Non-blank mentions, deduplicated case-insensitively, capped, first-seen order.

    Case folding is the right key because the underlying lookup itself matches
    titles case-insensitively: "Atlas" and "atlas" would resolve to the same
    node, so querying both is pure cost.
    """

    seen: dict[str, str] = {}
    for mention in mentions:
        text = mention.strip()
        if not text:
            continue
        seen.setdefault(text.casefold(), text)
        if len(seen) == MAX_MENTIONS:
            break
    return tuple(seen.values())


def _confidence(node: GraphNodeRecord, mention: str) -> float:
    """How exactly ``node`` matches ``mention``; 0 when it does not match at all."""

    if mention in {node.node_key, node.logical_id}:
        return _IDENTIFIER_CONFIDENCE
    if node.title is None:
        return 0.0
    if node.title == mention:
        return _EXACT_TITLE_CONFIDENCE
    if node.title.casefold() == mention.casefold():
        return _FOLDED_TITLE_CONFIDENCE
    return 0.0


class GraphMemoryEntityResolver:
    """Link memory mentions to curated graph node ids, one tenant at a time.

    ``ResolvedEntity.entity_id`` is always the matched node's ``node_key``, so
    an anchor can be handed straight back to graph traversal or to
    ``RetrievalRequest.graph_seed_node_keys`` as a seed.
    """

    def __init__(
        self,
        lookup: GraphEntityLookup,
        *,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        max_mentions: int = MAX_MENTIONS,
    ) -> None:
        self._lookup = lookup
        self._min_confidence = min_confidence
        self._max_mentions = max_mentions

    async def resolve_entities(
        self,
        mentions: Sequence[str],
        *,
        tenant_id: str,
    ) -> tuple[ResolvedEntity, ...]:
        """Resolve ``mentions`` inside ``tenant_id``, never raising.

        Mentions are deduplicated case-insensitively and capped at
        ``max_mentions``; each survivor is looked up concurrently. A mention
        the tenant's graph does not know, or one whose match falls below
        ``min_confidence``, is simply absent from the result.
        """

        selected = _dedupe(mentions)[: self._max_mentions]
        if not selected or not tenant_id.strip():
            return ()
        try:
            access = AccessContext(
                principal_id=_RESOLVER_PRINCIPAL,
                tenant_id=TenantId(tenant_id),
            )
            resolved = await asyncio.gather(
                *(self._resolve_one(mention, access) for mention in selected)
            )
        except Exception:
            logger.warning(
                "Resolving memory entity mentions failed for tenant=%s; %s",
                tenant_id,
                _UNAVAILABLE,
                exc_info=True,
            )
            return ()
        return tuple(entity for entity in resolved if entity is not None)

    async def _resolve_one(
        self,
        mention: str,
        access: AccessContext,
    ) -> ResolvedEntity | None:
        """Look one mention up, or ``None`` when the graph cannot confirm it.

        Tenant scoping is the ``access`` this method is handed, which the graph
        service turns into the storage context every query filters on -- a
        resolver can therefore never read across tenants.
        """

        result = await self._lookup.search_graph_subgraph(
            GraphSubgraphQuery(
                start_node=mention,
                max_depth=_LOOKUP_MAX_DEPTH,
                max_nodes=_LOOKUP_MAX_NODES,
                direction=GraphDirection.BOTH,
            ),
            access=access,
        )
        scored = [(_confidence(node, mention), node) for node in result.graph.nodes]
        best = max(scored, default=None, key=lambda item: item[0])
        if best is None or best[0] < self._min_confidence:
            return None
        return ResolvedEntity(
            mention=mention,
            entity_id=best[1].node_key,
            confidence=best[0],
        )


async def build_memory_entity_resolver(
    settings: RuntimeSettings,
    retrieval_provider: GraphLookupProvider,
) -> MemoryEntityResolver | None:
    """Build the graph-backed entity resolver, or ``None`` when it cannot run.

    Returns ``None`` -- logged, never raised -- when
    ``HARBORRAG_MEMORY_ENTITY_LINKING`` is off, when retrieval cannot be
    reached, or when this deployment has no graph configured. The resolver owns
    nothing: retrieval is borrowed through ``retrieval_provider`` and is closed
    by whoever opened it.
    """

    if not settings.memory_entity_linking:
        logger.info("Memory entity linking is disabled; %s", _UNAVAILABLE)
        return None
    try:
        lookup = await retrieval_provider()
    except Exception as exc:  # noqa: BLE001 - memory must degrade, never fail chat
        logger.warning(
            "Memory entity resolver unavailable: retrieval error_type=%s; %s",
            type(exc).__name__,
            _UNAVAILABLE,
        )
        return None
    if not lookup.graph_retrieval_available:
        logger.warning("Memory entity resolver unavailable: no graph retrieval; %s", _UNAVAILABLE)
        return None
    return GraphMemoryEntityResolver(lookup)


__all__ = [
    "DEFAULT_MIN_CONFIDENCE",
    "MAX_MENTIONS",
    "GraphEntityLookup",
    "GraphLookupProvider",
    "GraphMemoryEntityResolver",
    "build_memory_entity_resolver",
]
