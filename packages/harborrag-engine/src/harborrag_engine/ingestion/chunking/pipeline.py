from __future__ import annotations

from dataclasses import replace

from harborrag_core.contracts.chunking import (
    JsonStructureSplitter,
    StructureSplitter,
    TextRefiner,
    TokenCounter,
)

from ..base import BaseChunker
from .config import ChunkingConfig, ChunkingLimits, ChunkingPlan, ChunkingProfile
from .errors import ChunkValidationError
from .hierarchy import ChunkHierarchyValidator
from .identity import ChunkIdentityService, content_fingerprint
from .identity.fingerprint import manifest_fingerprint
from .packing import CompatiblePeerMerger, TokenBudgetPacker
from .record_factory import CanonicalChunkFactory, CanonicalChunkInput
from .registry import ChunkStrategyRegistry
from .router import ChunkingRouter
from .schemas import (
    ChunkCandidate,
    ChunkingDiagnostics,
    ChunkingRequest,
    ChunkingResult,
    ChunkManifest,
    ChunkReference,
)
from .segmentation import OversizedUnitRefiner, TableChunker
from .strategies import (
    ConfluenceChunkingStrategy,
    DocumentChunkingStrategy,
    GenericChunkingStrategy,
    JiraChunkingStrategy,
    JsonChunkingStrategy,
)
from .validation import ChunkValidator


class ChunkingService(BaseChunker):
    """Synchronously create deterministic canonical chunks and diagnostics."""

    def __init__(
        self,
        *,
        config: ChunkingConfig,
        registry: ChunkStrategyRegistry,
        router: ChunkingRouter,
        token_counter: TokenCounter,
        refiner: OversizedUnitRefiner,
        packer: TokenBudgetPacker,
        peer_merger: CompatiblePeerMerger,
        identity_service: ChunkIdentityService | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._router = router
        self._token_counter = token_counter
        self._refiner = refiner
        self._packer = packer
        self._peer_merger = peer_merger
        self._identity = identity_service or ChunkIdentityService()
        self._record_factory = CanonicalChunkFactory(token_counter, self._identity)
        self._hierarchy_validator = ChunkHierarchyValidator()
        self._validator = ChunkValidator(token_counter)

    def chunk(
        self,
        request: ChunkingRequest,
        plan: ChunkingPlan | None = None,
    ) -> ChunkingResult:
        """Create and validate canonical chunks for one normalized document."""

        selected = self._router.select(request)
        strategy = self._registry.get(selected.strategy)
        configured_profile = self._config.profiles[selected.profile]
        profile = self._profile_for_plan(configured_profile, plan)
        strategy_version = plan.strategy_version if plan is not None else strategy.version

        source_units = strategy.create_units(request, profile)
        oversized_units = sum(unit.token_count > profile.maximum_tokens for unit in source_units)
        refined_units = self._refiner.refine(source_units, profile)
        packed = self._packer.pack(refined_units, profile)
        candidates = self._assign_local_parts(self._peer_merger.merge(packed, profile))
        configuration_hash = self._identity.configuration_hash(
            configuration_version=self._config.configuration_version,
            profile=profile,
            chunker_name=strategy.name,
            chunker_version=strategy_version,
        )

        content_hashes = tuple(content_fingerprint(candidate.content) for candidate in candidates)
        identities = tuple(
            self._identity.identify(
                document_id=request.document.id,
                document_version_id=request.artifact_revision_id,
                strategy_version=strategy_version,
                section_path=candidate.structural_path,
                structural_anchor=candidate.anchor,
                local_part_index=candidate.local_part_index,
                chunk_kind=self._record_factory.kind_for_role(candidate.role),
                content_hash=content_hashes[index],
            )
            for index, candidate in enumerate(candidates)
        )
        records = tuple(
            self._record_factory.build(
                CanonicalChunkInput(
                    request=request,
                    candidate=candidate,
                    identity=identities[ordinal],
                    content_hash=content_hashes[ordinal],
                    ordinal=ordinal,
                    previous=(identities[ordinal - 1] if ordinal > 0 else None),
                    next_=(identities[ordinal + 1] if ordinal + 1 < len(identities) else None),
                    strategy_name=strategy.name,
                    strategy_version=strategy_version,
                    profile=profile,
                    configuration_hash=configuration_hash,
                    contextualize_embeddings=(
                        plan.contextualize_embeddings if plan is not None else True
                    ),
                )
            )
            for ordinal, candidate in enumerate(candidates)
        )
        self._hierarchy_validator.validate(records)
        validation = self._validator.validate(
            records,
            request,
            profile,
            strategy_name=strategy.name,
        )
        if not validation.valid:
            raise ChunkValidationError("; ".join(validation.errors))

        references = tuple(
            ChunkReference(
                logical_chunk_id=str(record.logical_chunk_id),
                chunk_revision_id=str(record.chunk_revision_id),
                ordinal=record.ordinal,
                content_hash=record.content_hash,
                token_count=record.token_count or 0,
            )
            for record in records
        )
        manifest = ChunkManifest(
            tenant_id=request.tenant_id,
            artifact_id=request.artifact_id,
            artifact_revision_id=request.artifact_revision_id,
            chunker_name=strategy.name,
            chunker_version=strategy_version,
            configuration_hash=configuration_hash,
            chunks=references,
            total_token_count=sum(reference.token_count for reference in references),
            total_chunk_count=len(references),
            validation=validation,
            fingerprint=manifest_fingerprint(
                reference.chunk_revision_id for reference in references
            ),
        )
        diagnostics = ChunkingDiagnostics(
            strategy=strategy.name,
            profile=profile.name,
            source_units=len(source_units),
            oversized_units=oversized_units,
            forced_splits=sum(unit.forced_split for unit in refined_units),
            merged_units=max(len(refined_units) - len(candidates), 0),
            final_chunks=len(records),
            total_tokens=sum(record.token_count or 0 for record in records),
        )
        return ChunkingResult(
            artifact_id=request.artifact_id,
            artifact_revision_id=request.artifact_revision_id,
            strategy=strategy.name,
            profile=profile.name,
            profile_hash=configuration_hash,
            chunks=records,
            diagnostics=diagnostics,
            manifest=manifest,
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
            ),
            merge_small_peers=configured.merge_small_peers,
            preserve_sections=configured.preserve_sections,
            preserve_tables=configured.preserve_tables,
            preserve_code_blocks=configured.preserve_code_blocks,
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


def build_default_chunking_service(
    *,
    config: ChunkingConfig,
    token_counter: TokenCounter,
    refiner: TextRefiner,
    markdown_splitter: StructureSplitter | None = None,
    html_splitter: StructureSplitter | None = None,
    json_splitter: JsonStructureSplitter | None = None,
) -> ChunkingService:
    """Compose built-in policies around explicitly injected adapter ports.

    Missing optional structure providers remain ``None`` so their strategies
    use normalized parser elements. Adapter discovery belongs to the runtime
    composition root rather than the engine.
    """

    strategies = (
        GenericChunkingStrategy(token_counter, refiner),
        DocumentChunkingStrategy(
            token_counter,
            markdown_splitter=markdown_splitter,
            html_splitter=html_splitter,
        ),
        JiraChunkingStrategy(token_counter),
        ConfluenceChunkingStrategy(token_counter),
        JsonChunkingStrategy(token_counter, json_splitter),
    )
    registry = ChunkStrategyRegistry(strategies)
    table_chunker = TableChunker(token_counter, refiner)
    oversized_refiner = OversizedUnitRefiner(refiner, table_chunker)
    packer = TokenBudgetPacker(token_counter)
    return ChunkingService(
        config=config,
        registry=registry,
        router=ChunkingRouter(config),
        token_counter=token_counter,
        refiner=oversized_refiner,
        packer=packer,
        peer_merger=CompatiblePeerMerger(token_counter, packer),
    )
