"""Construction of the default provider-independent chunking graph."""

from __future__ import annotations

from harborrag_core.contracts.chunking import ChunkContextualizer, TextRefiner, TokenCounter

from ..config import ChunkingConfig
from ..records.contextualizer import ContextHeaderBudget, DefaultChunkContextualizer
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


def builtin_chunking_strategies(token_counter: TokenCounter) -> tuple[ChunkStrategy, ...]:
    """The maintained strategies shared by execution and processing identity."""

    return (
        CanonicalDocumentChunkingStrategy(token_counter),
        ConfluenceChunkingStrategy(token_counter),
        JiraChunkingStrategy(token_counter),
    )


def build_chunking_service(  # noqa: PLR0913 - keyword-only composition options
    *,
    config: ChunkingConfig,
    token_counter: TokenCounter,
    refiner: TextRefiner,
    additional_strategies: tuple[ChunkStrategy, ...] = (),
    contextualizer: ChunkContextualizer | None = None,
    reserve_context_tokens: bool = True,
) -> ChunkingService:
    """Compose maintained strategies and shared transformation stages.

    ``contextualizer`` renders the context header of embedded evidence text
    (default: :class:`DefaultChunkContextualizer`); its header tokens are
    reserved from the packing budget so embedded text stays within limits,
    unless ``reserve_context_tokens`` is false.
    """

    header_renderer = contextualizer or DefaultChunkContextualizer(token_counter)

    strategies = ChunkStrategyRegistry(
        (*builtin_chunking_strategies(token_counter), *additional_strategies)
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
        header_budget=(
            ContextHeaderBudget(header_renderer, token_counter) if reserve_context_tokens else None
        ),
    )
    return ChunkingService(
        candidate_pipeline,
        ChunkResultBuilder(
            token_counter,
            configuration_version=config.configuration_version,
            strategies=strategies,
            contextualizer=header_renderer,
        ),
    )
