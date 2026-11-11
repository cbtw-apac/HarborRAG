"""Optional semantic candidates with independent canonical-build validation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.indexing import VectorIndexRecord, VectorSearchQuery, VectorSearchResult
from harborrag_core.ingestion import DocumentIdentityBuilder
from harborrag_core.ports.storage import VectorRepositoryPort
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.retrieval_policy import TopologyRetrievalPolicy
from harborrag_core.topology.search import (
    ContextualSearchPort,
    RetrievalMode,
    TopologyDiagnostics,
    TopologyEvidence,
    TopologySearchPort,
)
from harborrag_engine.retrieval import ActiveVersionCandidateValidator
from harborrag_engine.retrieval.fusion import fuse_candidates as fuse_candidates
from harborrag_engine.topology.retrieval import LocalTopologySearch

from .contracts import RetrievalOptions
from .evidence import evidence_metadata
from .topology_validation import eligible_semantic_candidates

logger = logging.getLogger("harborrag.runtime.retrieval.topology")


@dataclass(frozen=True, slots=True)
class TopologyCandidates:
    flat: tuple[VectorSearchResult, ...]
    semantic: tuple[VectorSearchResult, ...] = ()
    evidence: dict[str, TopologyEvidence] = field(default_factory=dict)
    diagnostics: TopologyDiagnostics = field(default_factory=TopologyDiagnostics)
    semantic_weight: float = 0.5
    rrf_constant: int = 60

    @property
    def candidates(self) -> tuple[VectorSearchResult, ...]:
        return fuse_candidates(
            self.flat,
            self.semantic,
            semantic_weight=self.semantic_weight,
            rrf_constant=self.rrf_constant,
        )


class TopologyRetrieval:
    def __init__(
        self,
        repository: TopologySearchPort | None,
        vectors: VectorRepositoryPort,
        validator: ActiveVersionCandidateValidator,
        *,
        policy: TopologyRetrievalPolicy | None = None,
        contextual: ContextualSearchPort | None = None,
    ) -> None:
        self._repository = repository
        self._vectors = vectors
        self._validator = validator
        self._policy = policy or TopologyRetrievalPolicy()
        self._semantic_weight = self._policy.semantic_weight
        self._contextual = contextual

    async def prepare(
        self,
        query: str,
        candidates: tuple[VectorSearchResult, ...],
        *,
        options: RetrievalOptions,
        context: StorageOperationContext,
        dense_vector: tuple[float, ...] | None = None,
    ) -> TopologyCandidates:
        batch = TopologyCandidates(
            candidates,
            diagnostics=TopologyDiagnostics(
                mode=options.mode.value, policy_version=self._policy.version
            ),
            semantic_weight=self._semantic_weight,
            rrf_constant=self._policy.rrf_constant,
        )
        if options.mode == RetrievalMode.FLAT:
            return batch
        if options.filters is not None:
            return _fallback(batch, "filters_require_flat")
        if self._repository is None:
            return _fallback(batch, "topology_unavailable")
        try:
            async with asyncio.timeout(2.0):
                return await self._expand(query, batch, context, dense_vector)
        except TimeoutError:
            return _fallback(batch, "topology_timeout")
        except Exception as error:
            logger.warning(
                "Semantic expansion failed; returning authoritative flat results",
                extra={"error_code": type(error).__name__},
            )
            return _fallback(batch, "topology_unavailable")

    async def _expand(
        self,
        query: str,
        batch: TopologyCandidates,
        context: StorageOperationContext,
        dense_vector: tuple[float, ...] | None,
    ) -> TopologyCandidates:
        if self._repository is None:
            return batch
        chunks = tuple(
            str(item.payload["chunk_id"]) for item in batch.flat if item.payload.get("chunk_id")
        )
        expansion = await LocalTopologySearch(self._repository, self._policy).expand(
            query, chunks, tenant_id=str(context.tenant_id), access=context.access
        )
        evidence = {item.chunk_id: item for item in expansion.evidence}
        identity = DocumentIdentityBuilder()
        ids = tuple(identity.point_id(chunk_id=chunk_id) for chunk_id in evidence)
        records = await self._vectors.get_records("evidence", ids, context=context) if ids else []
        semantic = tuple(
            _candidate(record) for record in records if _matches(record, evidence, context)
        )
        if self._contextual is not None and dense_vector is not None:
            contextual = await self._contextual.search(
                VectorSearchQuery(
                    index_name="evidence",
                    vector=list(dense_vector),
                    top_k=self._policy.max_candidate_chunks,
                ),
                context=context,
            )
            semantic, evidence = _merge_contextual(
                semantic, evidence, contextual, rrf_constant=self._policy.rrf_constant
            )
        validated = await self._validator.validate(semantic) if semantic else None
        accepted = validated.accepted if validated is not None else ()
        diagnostics = TopologyDiagnostics(
            mode=batch.diagnostics.mode,
            fallback=None if accepted else "no_active_topology_evidence",
            seed_entities=expansion.seed_entities,
            assertions=expansion.assertions,
            candidates=len(accepted),
            rejected=len(evidence) - len(accepted),
            ambiguous_labels=expansion.ambiguous_labels,
            truncated=expansion.truncated,
            suppressed_entities=expansion.suppressed_entities,
            policy_version=self._policy.version,
        )
        order = {str(item.id): rank for rank, item in enumerate(semantic)}
        return replace(
            batch,
            semantic=tuple(sorted(accepted, key=lambda item: order[str(item.id)])),
            evidence=evidence,
            diagnostics=diagnostics,
        )

    async def finalize(
        self,
        batch: TopologyCandidates,
        loaded: list[tuple[VectorSearchResult, RetrievalResult]],
        *,
        context: StorageOperationContext,
    ) -> tuple[list[tuple[VectorSearchResult, RetrievalResult]], TopologyDiagnostics]:
        if not batch.semantic or self._repository is None:
            return loaded, batch.diagnostics
        try:
            semantic = await eligible_semantic_candidates(
                self._repository, batch.semantic, batch.evidence, context
            )
            rejected = len(batch.semantic) - len(semantic)
            diagnostics = replace(
                batch.diagnostics,
                rejected=batch.diagnostics.rejected + rejected,
                fallback="topology_changed" if rejected else batch.diagnostics.fallback,
            )
        except Exception as error:
            logger.warning(
                "Topology authority unavailable at final validation",
                extra={"error_code": type(error).__name__},
            )
            semantic = ()
            diagnostics = replace(batch.diagnostics, fallback="topology_validation_failed")
        fused = fuse_candidates(
            batch.flat,
            semantic,
            semantic_weight=batch.semantic_weight,
            rrf_constant=batch.rrf_constant,
        )
        loaded_by_id = {str(item.id): result for item, result in loaded}
        semantic_ids = {str(item.id) for item in semantic}
        output = []
        for candidate in fused:
            result = loaded_by_id.get(str(candidate.id))
            if result is None:
                continue
            metadata = {**result.metadata, "raw_score": candidate.raw_score}
            if str(candidate.id) in semantic_ids:
                evidence = batch.evidence[result.id]
                metadata.update(evidence_metadata(evidence))
            metadata["retrieval_ranks"] = _ranks(candidate, batch.flat, semantic)
            output.append((candidate, replace(result, score=candidate.score, metadata=metadata)))
        return output, diagnostics


def _matches(
    record: VectorIndexRecord,
    evidence: dict[str, TopologyEvidence],
    context: StorageOperationContext,
) -> bool:
    chunk_id = record.payload.get("chunk_id")
    support = evidence.get(chunk_id) if isinstance(chunk_id, str) else None
    return bool(
        support is not None
        and str(record.tenant_id) == str(context.tenant_id)
        and record.id == DocumentIdentityBuilder().point_id(chunk_id=support.chunk_id)
        and record.payload.get("document_id") == support.document_id
        and record.payload.get("document_version_id") == support.document_version_id
        and record.payload.get("record_kind") == "evidence"
        and _matches_spans(record, support)
    )


def _matches_spans(record: VectorIndexRecord, support: TopologyEvidence) -> bool:
    content = record.payload.get("content")
    if not isinstance(content, str):
        return False
    try:
        for span in support.spans:
            span.validate_content(content)
    except ValueError:
        return False
    return True


def _candidate(record: VectorIndexRecord) -> VectorSearchResult:
    return VectorSearchResult(id=record.id, score=0, raw_score=0, payload=record.payload)


def _fallback(batch: TopologyCandidates, reason: str) -> TopologyCandidates:
    return replace(batch, diagnostics=replace(batch.diagnostics, fallback=reason))


def _ranks(
    candidate: VectorSearchResult,
    flat: tuple[VectorSearchResult, ...],
    semantic: tuple[VectorSearchResult, ...],
) -> dict[str, int]:
    return {
        name: rank
        for name, ranking in (("flat", flat), ("semantic", semantic))
        for rank, item in enumerate(ranking, 1)
        if item.id == candidate.id
    }


def _merge_contextual(
    semantic: tuple[VectorSearchResult, ...],
    evidence: dict[str, TopologyEvidence],
    contextual: tuple[tuple[VectorSearchResult, TopologyEvidence], ...],
    *,
    rrf_constant: int = 60,
) -> tuple[tuple[VectorSearchResult, ...], dict[str, TopologyEvidence]]:
    contextual_candidates: list[VectorSearchResult] = []
    for candidate, support in contextual:
        chunk = str(candidate.payload.get("chunk_id", ""))
        if (
            chunk != support.chunk_id
            or not support.build_ids
            or candidate.id != DocumentIdentityBuilder().point_id(chunk_id=chunk)
            or candidate.payload.get("record_kind") != "evidence"
            or candidate.payload.get("document_id") != support.document_id
            or candidate.payload.get("document_version_id") != support.document_version_id
        ):
            continue
        contextual_candidates.append(candidate)
        previous = evidence.get(chunk)
        evidence[chunk] = (
            support
            if previous is None
            else replace(
                previous,
                build_ids=tuple(sorted(set(previous.build_ids) | set(support.build_ids))),
                derived_artifact_ids=tuple(
                    sorted(set(previous.derived_artifact_ids) | set(support.derived_artifact_ids))
                ),
                navigation_summaries=(
                    *previous.navigation_summaries,
                    *support.navigation_summaries,
                ),
            )
        )
    # Independent contextual and graph ranks receive equal weight in the
    # enrichment lane; neither inherits a systematic append-order penalty.
    return fuse_candidates(
        semantic, tuple(contextual_candidates), rrf_constant=rrf_constant
    ), evidence
