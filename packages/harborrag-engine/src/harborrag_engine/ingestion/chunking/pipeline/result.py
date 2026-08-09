"""Canonical record, manifest, and diagnostic assembly."""

from __future__ import annotations

from harborrag_core.contracts.chunking import TokenCounter

from ..errors import ChunkValidationError
from ..identity import ChunkIdentityBuilder, ChunkIdentityInput, content_fingerprint
from ..identity.fingerprint import manifest_fingerprint
from ..records import (
    CanonicalChunkFactory,
    CanonicalChunkInput,
    ChunkHierarchyValidator,
    ChunkValidator,
)
from ..schemas import (
    ChunkingDiagnostics,
    ChunkingRequest,
    ChunkingResult,
    ChunkManifest,
    ChunkReference,
)
from ..sources import ChunkStrategyRegistry
from .candidates import CandidatePipelineResult


class ChunkResultBuilder:
    """Assign deterministic identities and validate the final chunk set."""

    def __init__(
        self,
        token_counter: TokenCounter,
        *,
        configuration_version: str,
        strategies: ChunkStrategyRegistry,
        identity_builder: ChunkIdentityBuilder | None = None,
    ) -> None:
        self._configuration_version = configuration_version
        self._strategies = strategies
        self._identity = identity_builder or ChunkIdentityBuilder()
        self._record_factory = CanonicalChunkFactory(token_counter, self._identity)
        self._hierarchy_validator = ChunkHierarchyValidator()
        self._validator = ChunkValidator(token_counter)

    def build(
        self,
        request: ChunkingRequest,
        pipeline: CandidatePipelineResult,
    ) -> ChunkingResult:
        """Build records, enforce invariants, and summarize the chunking run."""

        configuration_hash = self._identity.configuration_hash(
            configuration_version=self._configuration_version,
            profile=pipeline.profile,
            chunker_name=pipeline.strategy_name,
            chunker_version=pipeline.strategy_version,
        )
        content_hashes = tuple(
            content_fingerprint(candidate.content) for candidate in pipeline.candidates
        )
        identities = tuple(
            self._identity.identify(
                ChunkIdentityInput(
                    document_id=request.document.id,
                    document_version_id=request.document_version_id,
                    strategy_version=pipeline.strategy_version,
                    section_path=candidate.structural_path,
                    structural_anchor=candidate.anchor,
                    local_part_index=candidate.local_part_index,
                    chunk_kind=self._record_factory.kind_for_role(candidate.role),
                    content_hash=content_hashes[index],
                )
            )
            for index, candidate in enumerate(pipeline.candidates)
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
                    strategy_name=pipeline.strategy_name,
                    strategy_version=pipeline.strategy_version,
                    profile=pipeline.profile,
                    configuration_hash=configuration_hash,
                    contextualize_embeddings=pipeline.contextualize_embeddings,
                )
            )
            for ordinal, candidate in enumerate(pipeline.candidates)
        )

        self._hierarchy_validator.validate(records)
        validation = self._validator.validate(
            records,
            request,
            pipeline.profile,
            source_validator=self._strategies.record_validator(pipeline.strategy_name),
            require_route=pipeline.route_enabled,
        )
        if not validation.valid:
            raise ChunkValidationError("; ".join(validation.errors))

        references = tuple(
            ChunkReference(
                logical_chunk_id=str(record.logical_chunk_id),
                chunk_id=str(record.chunk_id),
                ordinal=record.ordinal,
                content_hash=record.content_hash,
                token_count=record.token_count or 0,
            )
            for record in records
        )
        manifest = ChunkManifest(
            tenant_id=request.tenant_id,
            document_id=request.document.id,
            document_version_id=request.document_version_id,
            chunker_name=pipeline.strategy_name,
            chunker_version=pipeline.strategy_version,
            configuration_hash=configuration_hash,
            chunks=references,
            total_token_count=sum(reference.token_count for reference in references),
            total_chunk_count=len(references),
            validation=validation,
            fingerprint=manifest_fingerprint(reference.chunk_id for reference in references),
        )
        diagnostics = ChunkingDiagnostics(
            strategy=pipeline.strategy_name,
            profile=pipeline.profile.name,
            source_units=pipeline.source_unit_count,
            oversized_units=pipeline.oversized_unit_count,
            forced_splits=pipeline.forced_split_count,
            merged_units=max(pipeline.refined_unit_count - len(pipeline.candidates), 0),
            final_chunks=len(records),
            total_tokens=sum(record.token_count or 0 for record in records),
        )
        return ChunkingResult(
            document_id=request.document.id,
            document_version_id=request.document_version_id,
            strategy=pipeline.strategy_name,
            profile=pipeline.profile.name,
            profile_hash=configuration_hash,
            chunks=records,
            diagnostics=diagnostics,
            manifest=manifest,
        )
