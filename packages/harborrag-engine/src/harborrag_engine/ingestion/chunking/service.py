from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from harborrag_adapters.chunking import HarborChunk
from harborrag_core.contracts.chunking import (
    JsonStructureSplitter,
    StructureSplitter,
    TextRefiner,
    TokenCounter,
)
from harborrag_core.schemas.documents import (
    ChunkContext,
    ChunkRecord,
    ChunkSourceSpan,
)
from harborrag_core.schemas.ids import (
    ChunkId,
    DocumentId,
    DocumentVersionId,
    TenantId,
)

from ..base import BaseChunker
from .config import ChunkingConfig, ChunkingProfile
from .errors import ChunkValidationError
from .identity import ChunkIdentity, ChunkIdentityService, content_fingerprint
from .identity.fingerprint import manifest_fingerprint
from .packing import CompatiblePeerMerger, TokenBudgetPacker
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
        self._validator = ChunkValidator(token_counter)

    def chunk(self, request: ChunkingRequest) -> ChunkingResult:
        """Create and validate canonical chunks for one normalized document."""

        selected = self._router.select(request)
        strategy = self._registry.get(selected.strategy)
        profile = self._config.profiles[selected.profile]

        source_units = strategy.create_units(request, profile)
        oversized_units = sum(unit.token_count > profile.maximum_tokens for unit in source_units)
        refined_units = self._refiner.refine(source_units, profile)
        packed = self._packer.pack(refined_units, profile)
        candidates = self._assign_local_parts(self._peer_merger.merge(packed, profile))
        configuration_hash = self._identity.configuration_hash(
            configuration_version=self._config.configuration_version,
            profile=profile,
            chunker_name=strategy.name,
            chunker_version=strategy.version,
        )

        content_hashes = tuple(content_fingerprint(candidate.content) for candidate in candidates)
        identities = tuple(
            self._identity.identify(
                tenant_id=request.tenant_id,
                artifact_id=request.artifact_id,
                strategy_name=strategy.name,
                structural_anchor=candidate.anchor,
                local_part_index=candidate.local_part_index,
                role=candidate.role,
                content_hash=content_hashes[index],
                configuration_hash=configuration_hash,
                chunker_version=strategy.version,
            )
            for index, candidate in enumerate(candidates)
        )
        records = tuple(
            self._record(
                request=request,
                candidate=candidate,
                identity=identities[ordinal],
                content_hash=content_hashes[ordinal],
                ordinal=ordinal,
                previous=(identities[ordinal - 1] if ordinal > 0 else None),
                next_=(identities[ordinal + 1] if ordinal + 1 < len(identities) else None),
                strategy_name=strategy.name,
                strategy_version=strategy.version,
                profile=profile,
                configuration_hash=configuration_hash,
            )
            for ordinal, candidate in enumerate(candidates)
        )
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
            chunker_version=strategy.version,
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

    def _record(
        self,
        *,
        request: ChunkingRequest,
        candidate: ChunkCandidate,
        identity: ChunkIdentity,
        content_hash: str,
        ordinal: int,
        previous: ChunkIdentity | None,
        next_: ChunkIdentity | None,
        strategy_name: str,
        strategy_version: str,
        profile: ChunkingProfile,
        configuration_hash: str,
    ) -> ChunkRecord:
        span = candidate.source_span
        previous_id = ChunkId(previous.logical_chunk_id) if previous is not None else None
        next_id = ChunkId(next_.logical_chunk_id) if next_ is not None else None
        parent_title = self._parent_title(candidate.metadata)
        source_span = ChunkSourceSpan(
            start_offset=span.start_offset,
            end_offset=span.end_offset,
            start_line=span.start_line,
            end_line=span.end_line,
            page_start=span.page_start,
            page_end=span.page_end,
            source_element_ids=span.element_ids,
        )
        context = ChunkContext(
            title=request.document.title.strip() or None,
            structural_path=candidate.structural_path,
            parent_title=parent_title,
            previous_chunk_id=previous_id,
            next_chunk_id=next_id,
        )
        return ChunkRecord(
            id=ChunkId(identity.chunk_revision_id),
            logical_chunk_id=ChunkId(identity.logical_chunk_id),
            chunk_revision_id=ChunkId(identity.chunk_revision_id),
            tenant_id=TenantId(request.tenant_id),
            document_id=DocumentId(request.document.id),
            document_version_id=DocumentVersionId(request.artifact_revision_id),
            artifact_id=request.artifact_id,
            artifact_revision_id=request.artifact_revision_id,
            chunk_index=ordinal,
            ordinal=ordinal,
            role=candidate.role,
            content=candidate.content,
            content_hash=content_hash,
            token_count=self._token_counter.count(candidate.content),
            source_span=source_span,
            context=context,
            structural_path=candidate.structural_path,
            start_offset=span.start_offset,
            end_offset=span.end_offset,
            page_start=span.page_start,
            page_end=span.page_end,
            start_line=span.start_line,
            end_line=span.end_line,
            previous_chunk_id=previous_id,
            next_chunk_id=next_id,
            source_element_ids=span.element_ids,
            metadata={
                **candidate.metadata,
                "boundary_kind": candidate.boundary_kind.value,
                "forced_split": candidate.forced_split,
                "structural_anchor": candidate.anchor,
                "local_part_index": candidate.local_part_index,
                "chunker_name": strategy_name,
                "chunker_version": strategy_version,
                "profile_name": profile.name,
                "configuration_hash": configuration_hash,
                "source_kind": request.source_kind,
            },
        )

    @staticmethod
    def _parent_title(metadata: Mapping[str, object]) -> str | None:
        values = metadata.get("ancestor_titles") or metadata.get("breadcrumb")
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
            for value in reversed(values):
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None


def build_default_chunking_service(
    *,
    config: ChunkingConfig,
    token_counter: TokenCounter,
    refiner: TextRefiner,
    markdown_splitter: StructureSplitter | None = None,
    html_splitter: StructureSplitter | None = None,
    json_splitter: JsonStructureSplitter | None = None,
) -> ChunkingService:
    """Compose built-in policies around injected and available adapters.

    Explicit splitter arguments take precedence. Missing optional structure
    providers remain ``None`` so their strategies use normalized parser
    elements instead of making the base ingestion path dependency-sensitive.
    """

    if markdown_splitter is None and HarborChunk.available("markdown"):
        markdown_splitter = HarborChunk("markdown", token_counter)
    if html_splitter is None and HarborChunk.available("html"):
        html_splitter = HarborChunk("html", token_counter)
    if json_splitter is None and HarborChunk.available("json"):
        json_splitter = HarborChunk("json", token_counter)

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
