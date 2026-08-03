"""Candidate-stage orchestration for the canonical chunking pipeline."""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..config import ChunkingConfig, ChunkingLimits, ChunkingPlan, ChunkingProfile
from ..schemas import ChunkCandidate, ChunkingRequest
from ..sources import ChunkStrategyRegistry
from ..transforms import (
    CompatiblePeerMerger,
    OversizedUnitRefiner,
    RouteChunkPlanner,
    TokenBudgetPacker,
)


@dataclass(frozen=True, slots=True)
class ChunkTransforms:
    """Transformation stages shared by every source strategy."""

    refiner: OversizedUnitRefiner
    packer: TokenBudgetPacker
    peer_merger: CompatiblePeerMerger
    route_planner: RouteChunkPlanner


@dataclass(frozen=True, slots=True)
class CandidatePipelineResult:
    """Candidate output plus the facts needed to build records and diagnostics."""

    strategy_name: str
    strategy_version: str
    profile: ChunkingProfile
    candidates: tuple[ChunkCandidate, ...]
    route_enabled: bool
    contextualize_embeddings: bool
    source_unit_count: int
    oversized_unit_count: int
    refined_unit_count: int
    forced_split_count: int


class ChunkCandidatePipeline:
    """Apply source strategy, refinement, packing, merging, and route planning."""

    def __init__(
        self,
        *,
        config: ChunkingConfig,
        strategies: ChunkStrategyRegistry,
        transforms: ChunkTransforms,
    ) -> None:
        self._config = config
        self._strategies = strategies
        self._transforms = transforms

    def run(
        self,
        request: ChunkingRequest,
        plan: ChunkingPlan | None,
    ) -> CandidatePipelineResult:
        """Produce ordered route/evidence candidates for one document."""

        configured_profile = self._config.profile_for(
            request.connector_type,
            request.profile_name,
        )
        strategy = self._strategies.get(configured_profile.strategy)
        profile = self._profile_for_plan(configured_profile, plan)
        strategy_version = plan.strategy_version if plan is not None else strategy.version

        source_units = strategy.create_units(request, profile)
        refined_units = self._transforms.refiner.refine(source_units, profile)
        packed = self._transforms.packer.pack(refined_units, profile)
        evidence_candidates = self._transforms.peer_merger.merge(packed, profile)

        include_evidence = plan.create_evidence_chunks if plan is not None else True
        route_enabled = self._config.create_route_chunks and (
            plan.create_route_chunks if plan is not None else True
        )
        candidates = evidence_candidates if include_evidence else ()
        candidates = self._transforms.route_planner.prepend(
            request,
            candidates or evidence_candidates,
            enabled=route_enabled,
        )
        if not include_evidence and route_enabled:
            candidates = candidates[:1]

        return CandidatePipelineResult(
            strategy_name=strategy.name,
            strategy_version=strategy_version,
            profile=profile,
            candidates=self._assign_local_parts(candidates),
            route_enabled=route_enabled,
            contextualize_embeddings=(plan.contextualize_embeddings if plan is not None else True),
            source_unit_count=len(source_units),
            oversized_unit_count=sum(
                unit.token_count > profile.maximum_tokens for unit in source_units
            ),
            refined_unit_count=len(refined_units),
            forced_split_count=sum(unit.forced_split for unit in refined_units),
        )

    @staticmethod
    def _profile_for_plan(
        configured: ChunkingProfile,
        plan: ChunkingPlan | None,
    ) -> ChunkingProfile:
        if plan is None:
            return configured
        return ChunkingProfile(
            name=plan.profile,
            strategy=configured.strategy,
            limits=ChunkingLimits(
                minimum_tokens=plan.minimum_tokens,
                target_tokens=plan.target_tokens,
                maximum_tokens=plan.hard_maximum_tokens,
                overlap_tokens=0,
                soft_maximum_tokens=plan.soft_maximum_tokens,
            ),
            merge_small_peers=configured.merge_small_peers,
            preserve_sections=configured.preserve_sections,
            repeat_table_headers=configured.repeat_table_headers,
        )

    @staticmethod
    def _assign_local_parts(
        candidates: tuple[ChunkCandidate, ...],
    ) -> tuple[ChunkCandidate, ...]:
        occurrences: dict[str, int] = {}
        output: list[ChunkCandidate] = []
        for candidate in candidates:
            local_part = occurrences.get(candidate.anchor, 0)
            occurrences[candidate.anchor] = local_part + 1
            output.append(replace(candidate, local_part_index=local_part))
        return tuple(output)
