"""Bounded permission-scoped incidence discovery and query-compatible typed paths."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field, replace

from harborrag_core.security import AccessContext
from harborrag_core.topology.extraction import EvidenceSpan
from harborrag_core.topology.records import CanonicalAssertion, CanonicalMention
from harborrag_core.topology.retrieval_policy import TopologyRetrievalPolicy
from harborrag_core.topology.search import (
    EvidencePath,
    TopologyEvidence,
    TopologyExpansion,
    TopologySearchPort,
)

_READ_LIMIT = 128
_RELATION_TERMS = {
    "owns": {"owns", "owner", "ownership", "owned", "responsible"},
    "depends_on": {"depends", "dependency", "dependencies", "dependent"},
    "supersedes": {"supersedes", "superseded", "replaces", "replacement"},
    "related_to": {"related", "relationship", "relationships"},
}


@dataclass(frozen=True)
class _Route:
    seed: CanonicalMention
    entities: tuple[str, ...]
    assertions: tuple[CanonicalAssertion, ...] = ()


@dataclass
class _Expansion:
    routes: dict[str, _Route]
    evidence: dict[str, TopologyEvidence] = field(default_factory=dict)
    assertions: set[str] = field(default_factory=set)
    truncated: bool = False
    suppressed: int = 0


class LocalTopologySearch:
    def __init__(
        self, repository: TopologySearchPort, policy: TopologyRetrievalPolicy | None = None
    ) -> None:
        self._repository = repository
        self._policy = policy or TopologyRetrievalPolicy()

    async def expand(
        self,
        query: str,
        chunk_ids: tuple[str, ...],
        *,
        tenant_id: str,
        access: AccessContext | None = None,
    ) -> TopologyExpansion:
        if access is None or str(access.tenant_id) != tenant_id:
            return TopologyExpansion()
        seed_mentions = (
            await self._repository.active_mentions(
                tenant_id,
                chunk_ids=chunk_ids[: self._policy.seed_chunks],
                limit=_READ_LIMIT,
                access=access,
            )
            if chunk_ids
            else ()
        )
        # A name found in a chunk is not an identity proof. Only explicit query
        # labels can seed a name lookup, and ambiguous matches remain separate.
        matches = await self._repository.active_mentions(
            tenant_id, labels=(query.strip(),), limit=_READ_LIMIT, access=access
        )
        label_seeds, ambiguous = _unambiguous(matches, truncated=len(matches) >= _READ_LIMIT)
        seeds = _seeds((*seed_mentions, *label_seeds), tenant_id, self._policy.max_entities)
        state = _Expansion(
            {key: _Route(mention, (key,)) for key, mention in seeds.items()},
            truncated=max(len(seed_mentions), len(matches)) >= _READ_LIMIT,
        )
        for route in state.routes.values():
            _add(state.evidence, _supported_evidence(route.seed, route))
        # One passage incidence round, independent of typed assertion availability.
        await self._mentions(state, tuple(state.routes), access)
        frontier = tuple(state.routes)
        predicates = compatible_predicates(query)
        for _ in range(self._policy.typed_relation_hops):
            if not frontier:
                break
            frontier = await self._typed_round(state, frontier, predicates, access)
            await self._mentions(state, frontier, access)
        evidence = tuple(state.evidence.values())
        return TopologyExpansion(
            evidence=evidence[: self._policy.max_candidate_chunks],
            seed_entities=len(seeds),
            assertions=len(state.assertions),
            ambiguous_labels=ambiguous,
            truncated=state.truncated or len(evidence) > self._policy.max_candidate_chunks,
            suppressed_entities=state.suppressed,
        )

    async def _mentions(
        self, state: _Expansion, entity_ids: tuple[str, ...], access: AccessContext
    ) -> None:
        for entity_id in entity_ids:
            mentions = await self._repository.active_mentions(
                str(access.tenant_id),
                entity_ids=(entity_id,),
                limit=self._policy.max_entity_mentions + 1,
                access=access,
            )
            if len(mentions) > self._policy.max_entity_mentions:
                state.suppressed += 1
                state.truncated = True
                continue
            for mention in mentions:
                if mention.tenant_id == str(access.tenant_id) and mention.entity_id == entity_id:
                    _add(state.evidence, _supported_evidence(mention, state.routes[entity_id]))
            if len(state.evidence) >= self._policy.max_candidate_chunks:
                state.truncated = True
                return

    async def _typed_round(
        self,
        state: _Expansion,
        frontier: tuple[str, ...],
        predicates: frozenset[str],
        access: AccessContext,
    ) -> tuple[str, ...]:
        assertions = await self._repository.active_assertions(
            str(access.tenant_id), entity_ids=frontier, limit=_READ_LIMIT, access=access
        )
        state.truncated |= len(assertions) >= _READ_LIMIT
        added: dict[str, _Route] = {}
        for assertion in assertions:
            if assertion.tenant_id != str(access.tenant_id):
                continue
            if predicates and assertion.observation.predicate not in predicates:
                continue
            _follow_assertion(state, added, assertion, frontier, self._policy.max_entities)
        state.routes.update(added)
        return tuple(added)


def compatible_predicates(query: str) -> frozenset[str]:
    """Uncertain routing allows bounded traversal; never disables direct retrieval."""
    words = set(re.findall(r"\w+", query.casefold()))
    return frozenset(predicate for predicate, terms in _RELATION_TERMS.items() if words & terms)


def _follow_assertion(
    state: _Expansion,
    added: dict[str, _Route],
    assertion: CanonicalAssertion,
    frontier: tuple[str, ...],
    max_entities: int,
) -> None:
    for source, target in (
        (assertion.subject_entity_id, assertion.object_entity_id),
        (assertion.object_entity_id, assertion.subject_entity_id),
    ):
        if source not in frontier:
            continue
        route = state.routes[source]
        if assertion.assertion_id in {item.assertion_id for item in route.assertions}:
            continue
        if (
            target not in state.routes
            and target not in added
            and len(state.routes) + len(added) >= max_entities
        ):
            state.truncated = True
            continue
        extended = _Route(route.seed, (*route.entities, target), (*route.assertions, assertion))
        state.assertions.add(assertion.assertion_id)
        _add(state.evidence, _supported_evidence(assertion, extended))
        if target in state.routes or target in added:
            continue
        added[target] = extended


def _unambiguous(
    mentions: tuple[CanonicalMention, ...], *, truncated: bool
) -> tuple[tuple[CanonicalMention, ...], int]:
    groups: dict[str, list[CanonicalMention]] = defaultdict(list)
    for mention in mentions:
        groups[mention.observation.name.casefold()].append(mention)
    ambiguous = sum(len({m.entity_id for m in values}) > 1 for values in groups.values())
    if truncated:
        return (), ambiguous
    return tuple(
        mention
        for values in groups.values()
        if len({m.entity_id for m in values}) == 1
        for mention in values
    ), ambiguous


def _seeds(
    mentions: tuple[CanonicalMention, ...], tenant_id: str, limit: int
) -> dict[str, CanonicalMention]:
    output: dict[str, CanonicalMention] = {}
    for mention in mentions:
        if mention.tenant_id == tenant_id:
            output.setdefault(mention.entity_id, mention)
        if len(output) == limit:
            break
    return output


def _supported_evidence(
    item: CanonicalMention | CanonicalAssertion, route: _Route
) -> TopologyEvidence:
    builds = tuple(
        sorted({route.seed.build_id, item.build_id, *(a.build_id for a in route.assertions)})
    )
    assertion_ids = tuple(dict.fromkeys(a.assertion_id for a in route.assertions))
    return TopologyEvidence(
        item.chunk_id,
        item.document_id,
        item.document_version_id,
        builds,
        assertion_ids,
        (item.observation.span,),
        paths=(
            EvidencePath(
                route.seed.chunk_id,
                item.chunk_id,
                route.entities,
                assertion_ids,
                builds,
                "typed_path" if route.assertions else "mention_incidence",
            ),
        ),
        assertions=route.assertions,
    )


def _add(output: dict[str, TopologyEvidence], value: TopologyEvidence) -> None:
    previous = output.get(value.chunk_id)
    if previous is None:
        output[value.chunk_id] = value
        return
    assertions = {a.assertion_id: a for a in (*previous.assertions, *value.assertions)}
    spans: dict[tuple[int, int, str], EvidenceSpan] = {
        (span.start, span.end, span.quote): span for span in (*previous.spans, *value.spans)
    }
    output[value.chunk_id] = replace(
        previous,
        build_ids=tuple(sorted(set(previous.build_ids) | set(value.build_ids))),
        assertion_ids=tuple(sorted(set(previous.assertion_ids) | set(value.assertion_ids))),
        spans=tuple(spans.values()),
        paths=tuple(dict.fromkeys((*previous.paths, *value.paths))),
        assertions=tuple(assertions.values()),
    )
