"""Anchor conversation memories to the curated document knowledge graph.

Conversation memory is *never* written into the document knowledge graph. It
is only linked to it: extraction asks a ``MemoryEntityResolver`` to map the
surface forms the model proposed onto graph node ids, stores those ids on
``Memory.entity_ids``, and recall boosts the memories whose ids overlap the
entities this turn's document retrieval actually surfaced. The graph stays the
curated system of record for entities; memory only holds references into it.

Both helpers here are total functions of their arguments, and neither ever
raises into extraction or recall: a resolver that fails yields no anchors, and
a fact with no resolvable mentions is still stored.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from harborrag_core.ports.memory import MemoryEntityResolver

from .facts import ExtractedFact
from .result import MemoryContext

logger = logging.getLogger(__name__)


def unique_ids(entity_ids: Sequence[str]) -> tuple[str, ...]:
    """Return the non-blank ids of ``entity_ids``, deduplicated in first-seen order."""

    return tuple(dict.fromkeys(entity_id.strip() for entity_id in entity_ids if entity_id.strip()))


async def resolve_entity_ids(
    resolver: MemoryEntityResolver,
    mentions: Sequence[str],
    *,
    tenant_id: str,
    confidence_floor: float,
) -> tuple[str, ...]:
    """Resolve ``mentions`` to graph node ids at or above ``confidence_floor``.

    A mention the graph cannot resolve, or one resolved below the floor, is
    dropped: an unanchored mention is noise for ranking, not a reason to lose
    the fact that carried it. A raising resolver is logged at WARNING and
    yields no anchors at all, so extraction still stores the fact.
    """

    if not mentions:
        return ()
    try:
        resolved = await resolver.resolve_entities(tuple(mentions), tenant_id=tenant_id)
    except Exception:
        logger.warning("resolving memory entity mentions failed", exc_info=True)
        return ()
    return unique_ids(
        [entity.entity_id for entity in resolved if entity.confidence >= confidence_floor]
    )


async def anchored_fact(
    fact: ExtractedFact,
    *,
    resolver: MemoryEntityResolver | None,
    tenant_id: str,
    confidence_floor: float,
) -> ExtractedFact:
    """Return ``fact`` with its proposed mentions replaced by resolved graph ids.

    Without a resolver the fact is returned untouched, so the proposed
    mentions are stored verbatim exactly as they were before anchoring
    existed.
    """

    if resolver is None:
        return fact
    return replace(
        fact,
        entities=await resolve_entity_ids(
            resolver,
            fact.entities,
            tenant_id=tenant_id,
            confidence_floor=confidence_floor,
        ),
    )


def recalled_entity_ids(context: MemoryContext) -> tuple[str, ...]:
    """Return the graph entities the recalled memories reference, in recall order.

    This is the seed set for a graph-anchored follow-up search: the union of
    ``Memory.entity_ids`` across ``context.recalled``, deduplicated and kept
    in the order recall ranked the memories.
    """

    return unique_ids([entity_id for memory in context.recalled for entity_id in memory.entity_ids])


__all__ = ["anchored_fact", "recalled_entity_ids", "resolve_entity_ids", "unique_ids"]
