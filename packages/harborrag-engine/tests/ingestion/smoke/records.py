from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

from harborrag_core.schemas.documents import ChunkContext, ChunkRecord, ChunkSourceSpan
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_engine.ingestion.chunking.schemas import (
    ChunkingDiagnostics,
    ChunkingResult,
    ChunkManifest,
    ChunkReference,
    ChunkValidationResult,
)
from harborrag_engine.ingestion.indexing import IndexingConfig, IndexingRequest


class CharacterTokenCounter:
    """Use a conservative deterministic count for the short smoke inputs."""

    def count(self, text: str) -> int:
        """Count characters so provider batches remain safely bounded."""

        return len(text)


def build_indexing_request(config: IndexingConfig) -> IndexingRequest:
    """Create validated canonical chunking output at the indexing boundary."""

    suffix = uuid4().hex[:12]
    tenant_id = f"smoke-{suffix}"
    artifact_id = f"artifact-{suffix}"
    artifact_revision_id = f"artifact-revision-{suffix}"
    contents = (
        "HarborRAG stages new vectors before generation activation.",
        "Deterministic graph projection links revisions, sections, and chunks.",
    )
    references: list[ChunkReference] = []
    records: list[ChunkRecord] = []
    for ordinal, content in enumerate(contents):
        logical_id = _digest(f"{artifact_id}:section:{ordinal}")
        content_hash = _digest(content)
        revision_id = _digest(f"{logical_id}:{content_hash}:smoke-v1")
        reference = ChunkReference(
            logical_chunk_id=logical_id,
            chunk_revision_id=revision_id,
            ordinal=ordinal,
            content_hash=content_hash,
            token_count=len(content),
            body_uri=f"smoke://chunks/{revision_id}",
        )
        references.append(reference)
        path = ("Indexing smoke", f"Section {ordinal + 1}")
        records.append(
            ChunkRecord(
                id=revision_id,
                logical_chunk_id=logical_id,
                chunk_revision_id=revision_id,
                tenant_id=tenant_id,
                document_id=artifact_id,
                document_version_id=artifact_revision_id,
                artifact_id=artifact_id,
                artifact_revision_id=artifact_revision_id,
                chunk_index=ordinal,
                ordinal=ordinal,
                role="content",
                content=content,
                content_hash=content_hash,
                token_count=len(content),
                source_span=ChunkSourceSpan(start_line=ordinal + 1, end_line=ordinal + 1),
                context=ChunkContext(
                    title="HarborRAG indexing smoke",
                    structural_path=path,
                ),
                structural_path=path,
                start_line=ordinal + 1,
                end_line=ordinal + 1,
                metadata={"source_kind": "document", "smoke_test": True},
            )
        )

    manifest = ChunkManifest(
        tenant_id=tenant_id,
        artifact_id=artifact_id,
        artifact_revision_id=artifact_revision_id,
        chunker_name="smoke-document",
        chunker_version="1",
        configuration_hash=_digest("smoke-chunker-v1"),
        chunks=tuple(references),
        total_token_count=sum(item.token_count for item in references),
        total_chunk_count=len(references),
        validation=ChunkValidationResult(valid=True),
        fingerprint=_digest(":".join(item.chunk_revision_id for item in references)),
    )
    chunking = ChunkingResult(
        artifact_id=artifact_id,
        artifact_revision_id=artifact_revision_id,
        strategy="smoke-document",
        profile="document",
        profile_hash=manifest.configuration_hash,
        chunks=tuple(records),
        diagnostics=ChunkingDiagnostics(
            strategy="smoke-document",
            profile="document",
            source_units=len(records),
            oversized_units=0,
            forced_splits=0,
            merged_units=0,
            final_chunks=len(records),
            total_tokens=manifest.total_token_count,
        ),
        manifest=manifest,
    )
    return IndexingRequest(
        chunking=chunking,
        generation_id=f"generation-{suffix}",
        config=config,
        context=StorageOperationContext(tenant_id=tenant_id),
    )


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()
