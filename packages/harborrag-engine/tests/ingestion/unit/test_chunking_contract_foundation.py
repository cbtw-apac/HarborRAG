from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_core.chunking import (
    ChunkHierarchy,
    ChunkKind,
    ChunkRecord,
    ChunkSecurity,
    ConnectorType,
    DocumentKind,
)
from harborrag_core.domain.element import DocumentElement
from harborrag_core.schemas.ids import ChunkId
from harborrag_engine.ingestion import HarborChunker
from harborrag_engine.ingestion.chunking import (
    ChunkIdentityBuilder,
    ChunkingDiagnostics,
    ChunkingPlan,
    ChunkingResult,
    ChunkingStatistics,
    ChunkManifest,
    ChunkReference,
    ChunkValidationResult,
    InvalidChunkingPlanError,
)
from harborrag_engine.ingestion.chunking.identity import content_fingerprint

from .chunking_helpers import make_document, make_request


class FakeCanonicalChunker(HarborChunker):
    """Minimal contract proof with no adapter, connector, or storage dependency."""

    def __init__(self) -> None:
        self._identity = ChunkIdentityBuilder()

    def chunk(self, request, plan) -> ChunkingResult:
        content = request.document.content[0].content or ""
        content_hash = content_fingerprint(content)
        identity = self._identity.identify(
            document_id=request.document.id,
            document_version_id=request.artifact_revision_id,
            strategy_version=plan.strategy_version,
            section_path=("Body",),
            structural_anchor=request.document.content[0].id,
            local_part_index=0,
            chunk_kind=ChunkKind.EVIDENCE,
            content_hash=content_hash,
        )
        record = ChunkRecord(
            strategy_version=plan.strategy_version,
            chunk_id=ChunkId(identity.chunk_id),
            logical_chunk_id=ChunkId(identity.logical_chunk_id),
            content_hash=content_hash,
            connector_type=ConnectorType.LOCAL,
            document_kind=DocumentKind.LOCAL_FILE,
            chunk_kind=ChunkKind.EVIDENCE,
            tenant_id=request.tenant_id,
            connection_id="fake",
            source_scope=request.tenant_id,
            source_item_id=request.artifact_id,
            source_version=request.artifact_revision_id,
            document_id=request.document.id,
            document_version_id=request.artifact_revision_id,
            ordinal=0,
            content=content,
            contextual_prefix="Document: Contract",
            embedding_text=f"Document: Contract\n\n{content}",
            search_text=f"{request.document.id}\n{content}",
            token_count=len(content),
            hierarchy=ChunkHierarchy(
                section_path=("Body",),
                section_id=identity.section_id,
            ),
            security=ChunkSecurity(permission_set_id="permission-set:fake"),
        )
        reference = ChunkReference(
            logical_chunk_id=str(record.logical_chunk_id),
            chunk_revision_id=str(record.chunk_id),
            ordinal=0,
            content_hash=record.content_hash,
            token_count=record.token_count,
        )
        validation = ChunkValidationResult(valid=True)
        manifest = ChunkManifest(
            tenant_id=request.tenant_id,
            artifact_id=request.artifact_id,
            artifact_revision_id=request.artifact_revision_id,
            chunker_name="fake",
            chunker_version=plan.strategy_version,
            configuration_hash="chunk-config:fake",
            chunks=(reference,),
            total_token_count=record.token_count,
            total_chunk_count=1,
            validation=validation,
            fingerprint="chunk-manifest:fake",
        )
        return ChunkingResult(
            artifact_id=request.artifact_id,
            artifact_revision_id=request.artifact_revision_id,
            strategy="fake",
            profile=plan.profile,
            profile_hash=manifest.configuration_hash,
            chunks=(record,),
            diagnostics=ChunkingDiagnostics(
                strategy="fake",
                profile=plan.profile,
                source_units=1,
                oversized_units=0,
                forced_splits=0,
                merged_units=0,
                final_chunks=1,
                total_tokens=record.token_count,
            ),
            manifest=manifest,
        )


def test_chunking_plan_is_immutable_and_validates_common_limits() -> None:
    plan = ChunkingPlan(
        profile="contract",
        strategy_version="strategy-1",
        minimum_tokens=1,
        target_tokens=2,
        soft_maximum_tokens=3,
        hard_maximum_tokens=4,
    )

    assert plan.create_evidence_chunks
    assert plan.boundary_overlap_sentences == 0

    invalid_values = (
        {"profile": " "},
        {"strategy_version": ""},
        {"minimum_tokens": 0},
        {"minimum_tokens": 3, "target_tokens": 2},
        {"target_tokens": 4, "soft_maximum_tokens": 3},
        {"soft_maximum_tokens": 5, "hard_maximum_tokens": 4},
        {"boundary_overlap_sentences": -1},
    )
    for changes in invalid_values:
        values = {
            "profile": "contract",
            "strategy_version": "strategy-1",
            "minimum_tokens": 1,
            "target_tokens": 2,
            "soft_maximum_tokens": 3,
            "hard_maximum_tokens": 4,
            **changes,
        }
        try:
            ChunkingPlan(**values)
        except InvalidChunkingPlanError:
            continue
        raise AssertionError(f"invalid plan was accepted: {changes}")

    with pytest.raises(ValueError, match="must not be negative"):
        ChunkingStatistics(-1, 0, 0, 0, 0)


def test_fake_chunker_proves_canonical_contract_and_result_shape() -> None:
    request = make_request(make_document([DocumentElement("body", "paragraph", "contract")]))
    plan = ChunkingPlan(
        profile="contract",
        strategy_version="strategy-7",
        minimum_tokens=1,
        target_tokens=8,
        soft_maximum_tokens=9,
        hard_maximum_tokens=10,
    )
    chunker: HarborChunker = FakeCanonicalChunker()

    first = chunker.chunk(request, plan)
    repeated = chunker.chunk(request, plan)

    assert first == repeated
    assert first.document_id == request.document.id
    assert first.document_version_id == request.artifact_revision_id
    assert first.strategy_version == "strategy-7"
    assert first.warnings == ()
    assert first.statistics == ChunkingStatistics(0, 1, 0, len("contract"), 0)
    assert first.chunks[0].chunk_id == repeated.chunks[0].chunk_id


def test_fake_chunker_surfaces_invalid_canonical_chunk_validation() -> None:
    request = make_request(make_document([DocumentElement("body", "paragraph", "")]))
    plan = ChunkingPlan(
        profile="contract",
        strategy_version="strategy-1",
        minimum_tokens=1,
        target_tokens=2,
        soft_maximum_tokens=3,
        hard_maximum_tokens=4,
    )

    with pytest.raises(ValidationError, match="content must be non-empty"):
        FakeCanonicalChunker().chunk(request, plan)
