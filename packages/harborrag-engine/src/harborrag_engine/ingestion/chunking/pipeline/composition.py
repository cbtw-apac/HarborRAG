"""Construction of the default provider-independent chunking graph."""

from __future__ import annotations

from harborrag_core.contracts.chunking import TextRefiner, TokenCounter

from ..config import ChunkingConfig
from ..sources import (
    CanonicalDocumentChunkingStrategy,
    ChunkStrategy,
    ChunkStrategyRegistry,
    ConfluenceChunkingStrategy,
    JiraChunkingStrategy,
)
from ..transforms import (
    CompatiblePeerMerger,
    OversizedUnitRefiner,
    RouteChunkPlanner,
    TableRowSplitter,
    TokenBudgetPacker,
)
from .candidates import ChunkCandidatePipeline, ChunkTransforms
from .result import ChunkResultBuilder
from .service import ChunkingService


def build_chunking_service(
    *,
    config: ChunkingConfig,
    token_counter: TokenCounter,
    refiner: TextRefiner,
    additional_strategies: tuple[ChunkStrategy, ...] = (),
) -> ChunkingService:
    """Compose maintained strategies and shared transformation stages."""

    strategies = ChunkStrategyRegistry(
        (
            CanonicalDocumentChunkingStrategy(token_counter),
            ConfluenceChunkingStrategy(token_counter),
            JiraChunkingStrategy(token_counter),
            *additional_strategies,
        )
    )
    table_splitter = TableRowSplitter(token_counter, refiner)
    oversized_refiner = OversizedUnitRefiner(refiner, table_splitter)
    packer = TokenBudgetPacker(token_counter)
    candidate_pipeline = ChunkCandidatePipeline(
        config=config,
        strategies=strategies,
        transforms=ChunkTransforms(
            refiner=oversized_refiner,
            packer=packer,
            peer_merger=CompatiblePeerMerger(token_counter, packer),
            route_planner=RouteChunkPlanner(token_counter),
        ),
    )
    return ChunkingService(
        candidate_pipeline,
        ChunkResultBuilder(
            token_counter,
            configuration_version=config.configuration_version,
            strategies=strategies,
        ),
    )
