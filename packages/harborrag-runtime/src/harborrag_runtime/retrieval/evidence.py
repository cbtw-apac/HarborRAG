"""Protected direct evidence, whole-passage budgets, and qualified evidence bundles."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, replace

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.indexing import VectorSearchResult
from harborrag_core.topology.records import CanonicalAssertion
from harborrag_core.topology.retrieval_policy import TopologyRetrievalPolicy
from harborrag_core.topology.search import (
    EvidenceBundle,
    EvidencePath,
    TopologyDiagnostics,
    TopologyEvidence,
)


def select_evidence(
    results: list[RetrievalResult],
    direct: tuple[VectorSearchResult, ...],
    *,
    top_k: int,
    policy: TopologyRetrievalPolicy,
) -> tuple[list[RetrievalResult], int, int]:
    """Reserve direct slots proportionally (4/10 by default); never truncate text.

    UTF-8 byte length of the serialized passage packet is a conservative token
    accounting unit for byte-tokenizing LLMs, including citation/path metadata.
    It deliberately overcounts; no tokenizer-specific precision is claimed.
    """
    by_id = {item.id: item for item in results}
    protected = min(
        top_k, math.ceil(top_k * policy.protected_direct_passages / policy.target_passages)
    )
    ordered = [
        by_id[str(item.payload.get("chunk_id"))]
        for item in direct
        if str(item.payload.get("chunk_id")) in by_id
    ]
    allocation = _EvidenceAllocation(policy.max_context_tokens)
    for candidate in ordered:
        if len(allocation.selected) >= protected:
            break
        allocation.take(candidate)
    for candidate in results:
        if len(allocation.selected) == top_k:
            break
        allocation.take(candidate)
    return allocation.selected, allocation.spent, allocation.excluded


@dataclass
class _EvidenceAllocation:
    limit: int
    selected: list[RetrievalResult] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    spent: int = 0
    excluded: int = 0

    def take(self, candidate: RetrievalResult) -> None:
        if candidate.id in self.seen:
            return
        self.seen.add(candidate.id)
        cost = len(json.dumps(asdict(candidate), ensure_ascii=False, default=str).encode("utf-8"))
        if self.spent + cost > self.limit:
            self.excluded += 1
            return
        self.selected.append(candidate)
        self.spent += cost


def build_evidence_bundle(
    results: tuple[RetrievalResult, ...],
    evidence: dict[str, TopologyEvidence],
    diagnostics: TopologyDiagnostics,
) -> EvidenceBundle:
    assertions: dict[str, CanonicalAssertion] = {}
    paths: dict[EvidencePath, None] = {}
    navigation: dict[str, dict[str, object]] = {}
    gaps: list[str] = []
    for result in results:
        support = evidence.get(result.id)
        if support is None or not result.metadata.get("topology_build_ids"):
            continue
        for assertion in support.assertions:
            assertions[assertion.assertion_id] = assertion
        paths.update(dict.fromkeys(support.paths))
        for summary in support.navigation_summaries:
            navigation[str(summary.get("parent_key"))] = summary
    if diagnostics.fallback:
        gaps.append(diagnostics.fallback)
    if diagnostics.truncated:
        gaps.append("bounded_graph_coverage")
    if diagnostics.budget_excluded:
        gaps.append("context_budget_excluded_passages")
    if any(a.chunk_id not in {r.id for r in results} for a in assertions.values()):
        gaps.append("path_support_passages_not_selected")
    return EvidenceBundle(
        original_passages=results,
        relevant_assertions=tuple(assertions.values()),
        evidence_paths=tuple(paths),
        coverage_gaps=tuple(gaps),
        conflicting_evidence=_conflicts(tuple(assertions.values())),
        navigation_summaries=tuple(navigation.values()),
    )


def _conflicts(assertions: tuple[CanonicalAssertion, ...]) -> tuple[tuple[str, ...], ...]:
    # Do not choose a winner by ingestion time. These are candidate conflicts,
    # grouped conservatively only when the explicit temporal qualification agrees.
    groups: dict[tuple[str, ...], list[CanonicalAssertion]] = {}
    for item in assertions:
        key = (
            item.subject_entity_id,
            item.observation.predicate,
            item.object_entity_id,
            json.dumps(
                item.observation.model_dump(
                    mode="json",
                    include={
                        "modality",
                        "time_qualifier",
                        "valid_from",
                        "valid_to",
                        "temporal_precision",
                        "attribution",
                        "qualifiers",
                    },
                ),
                sort_keys=True,
            ),
        )
        groups.setdefault(key, []).append(item)
    return tuple(
        tuple(item.assertion_id for item in values)
        for values in groups.values()
        if len({item.observation.polarity for item in values}) > 1
    )


def evidence_metadata(evidence: TopologyEvidence) -> dict[str, object]:
    return {
        "topology_build_ids": evidence.build_ids,
        "topology_assertion_ids": evidence.assertion_ids,
        "derived_artifact_ids": evidence.derived_artifact_ids,
        "navigation_summaries": evidence.navigation_summaries,
        "evidence_paths": tuple(asdict(path) for path in evidence.paths),
        "qualified_assertions": tuple(item.model_dump(mode="json") for item in evidence.assertions),
        "retrieval_source": "canonical-topology+qdrant-authoritative",
    }


def budget_diagnostics(
    diagnostics: TopologyDiagnostics, tokens: int, excluded: int
) -> TopologyDiagnostics:
    return replace(diagnostics, context_tokens=tokens, budget_excluded=excluded)
