"""Schemas owned by the engine chunking stage."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from harborrag_core.chunking import ChunkKind
from harborrag_core.contracts.chunking import SourceSpan, SplitBoundaryKind
from harborrag_core.domain.normalized_document import Document
from harborrag_core.schemas.documents import ChunkRecord


def _freeze_metadata(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(values))


@dataclass(frozen=True, slots=True)
class ChunkUnit:
    """One parser- or strategy-derived structural unit before packing."""

    anchor: str
    content: str
    token_count: int
    role: str
    structural_path: tuple[str, ...]
    source_span: SourceSpan
    merge_group: str
    boundary_kind: SplitBoundaryKind = SplitBoundaryKind.PARAGRAPH
    hard_boundary_before: bool = False
    hard_boundary_after: bool = False
    forced_split: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.anchor.strip() or not self.merge_group.strip():
            raise ValueError("chunk unit anchor and merge_group must be non-empty")
        if not self.role.strip() or not self.content or not self.content.strip():
            raise ValueError("chunk unit role and content must be non-empty")
        if self.token_count < 1:
            raise ValueError("chunk unit token_count must be positive")
        if any(not part.strip() for part in self.structural_path):
            raise ValueError("chunk unit structural_path parts must be non-empty")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class ChunkCandidate:
    """One or more compatible units packed before stable identity is assigned."""

    anchor: str
    content: str
    token_count: int
    role: str
    structural_path: tuple[str, ...]
    source_span: SourceSpan
    units: tuple[ChunkUnit, ...]
    boundary_kind: SplitBoundaryKind
    metadata: Mapping[str, Any] = field(default_factory=dict)
    local_part_index: int = 0
    forced_split: bool = False

    def __post_init__(self) -> None:
        if not self.anchor.strip() or not self.content or not self.content.strip():
            raise ValueError("chunk candidate anchor and content must be non-empty")
        if not self.role.strip() or not self.units:
            raise ValueError("chunk candidate role and units must be non-empty")
        if self.token_count < 1 or self.local_part_index < 0:
            raise ValueError("candidate counts must be positive/non-negative")
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class ChunkReference:
    """Lightweight manifest reference to a separately persisted chunk body."""

    logical_chunk_id: str
    chunk_revision_id: str
    ordinal: int
    content_hash: str
    token_count: int
    body_uri: str | None = None

    def __post_init__(self) -> None:
        if not self.logical_chunk_id or not self.chunk_revision_id:
            raise ValueError("chunk reference identity values must be non-empty")
        if self.ordinal < 0 or self.token_count < 1:
            raise ValueError(
                "chunk reference ordinal must be non-negative and token_count positive"
            )
        if not self.content_hash:
            raise ValueError("chunk reference content_hash must be non-empty")


@dataclass(frozen=True, slots=True)
class ChunkValidationResult:
    """Persistable validation outcome for one generated manifest."""

    valid: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChunkManifest:
    """Lightweight, reproducible manifest for one artifact revision."""

    tenant_id: str
    artifact_id: str
    artifact_revision_id: str
    chunker_name: str
    chunker_version: str
    configuration_hash: str
    chunks: tuple[ChunkReference, ...]
    total_token_count: int
    total_chunk_count: int
    validation: ChunkValidationResult
    fingerprint: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.tenant_id,
                self.artifact_id,
                self.artifact_revision_id,
                self.chunker_name,
                self.chunker_version,
                self.configuration_hash,
                self.fingerprint,
            )
        ):
            raise ValueError("chunk manifest identity values must be non-empty")
        if self.total_chunk_count != len(self.chunks):
            raise ValueError("manifest total_chunk_count does not match references")
        if self.total_token_count != sum(chunk.token_count for chunk in self.chunks):
            raise ValueError("manifest total_token_count does not match references")


@dataclass(frozen=True, slots=True)
class ChunkingRequest:
    """Stable inputs and explicit routing hints for one artifact revision."""

    tenant_id: str
    artifact_id: str
    artifact_revision_id: str
    document: Document
    source_kind: str = ""
    content_type: str = ""
    profile_name: str | None = None

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.tenant_id,
                self.artifact_id,
                self.artifact_revision_id,
            )
        ):
            raise ValueError("chunking request identity values must be non-empty")
        source_kind = (self.source_kind.strip() or self.document.provenance.source).lower()
        content_type = (self.content_type.strip() or self._document_content_type()).lower()
        content_type = content_type.split(";", 1)[0].strip()
        if not source_kind.strip() or not content_type.strip():
            raise ValueError("chunking request routing values must be non-empty")
        if self.profile_name is not None and not self.profile_name.strip():
            raise ValueError("profile_name must be non-empty when provided")
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(self, "content_type", content_type)
        if self.profile_name is not None:
            object.__setattr__(self, "profile_name", self.profile_name.strip())

    def _document_content_type(self) -> str:
        for key in ("content_type", "media_type", "mime_type"):
            value = self.document.provenance.extra.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return self.document.content_type


@dataclass(frozen=True, slots=True)
class ChunkingDiagnostics:
    """Deterministic counters describing one chunking execution."""

    strategy: str
    profile: str
    source_units: int
    oversized_units: int
    forced_splits: int
    merged_units: int
    final_chunks: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class ChunkingStatistics:
    """Source-independent counters for one completed chunking result."""

    route_chunk_count: int
    evidence_chunk_count: int
    table_chunk_count: int
    total_token_count: int
    rejected_chunk_count: int

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.route_chunk_count,
                self.evidence_chunk_count,
                self.table_chunk_count,
                self.total_token_count,
                self.rejected_chunk_count,
            )
        ):
            raise ValueError("chunking statistics values must not be negative")


@dataclass(frozen=True, slots=True)
class ChunkingResult:
    """Canonical chunks, diagnostics, and their lightweight manifest."""

    artifact_id: str
    artifact_revision_id: str
    strategy: str
    profile: str
    profile_hash: str
    chunks: tuple[ChunkRecord, ...]
    diagnostics: ChunkingDiagnostics
    manifest: ChunkManifest
    document_id: str = field(init=False)
    document_version_id: str = field(init=False)
    strategy_version: str = field(init=False)
    warnings: tuple[str, ...] = field(init=False)
    statistics: ChunkingStatistics = field(init=False)

    def __post_init__(self) -> None:
        """Reject inconsistent records, diagnostics, and manifest references."""

        if not all(
            value.strip()
            for value in (
                self.artifact_id,
                self.artifact_revision_id,
                self.strategy,
                self.profile,
                self.profile_hash,
            )
        ):
            raise ValueError("chunking result identity values must be non-empty")
        if (
            self.artifact_id != self.manifest.artifact_id
            or self.artifact_revision_id != self.manifest.artifact_revision_id
            or self.strategy != self.manifest.chunker_name
            or self.profile_hash != self.manifest.configuration_hash
        ):
            raise ValueError("chunking result does not match its manifest")
        if (
            self.diagnostics.strategy != self.strategy
            or self.diagnostics.profile != self.profile
            or self.diagnostics.final_chunks != len(self.chunks)
            or self.diagnostics.total_tokens
            != sum(record.token_count or 0 for record in self.chunks)
        ):
            raise ValueError("chunking result diagnostics do not match its chunks")
        if len(self.chunks) != len(self.manifest.chunks):
            raise ValueError("chunking result records do not match manifest references")
        for record, reference in zip(self.chunks, self.manifest.chunks, strict=True):
            if (
                str(record.tenant_id) != self.manifest.tenant_id
                or record.artifact_id != self.artifact_id
                or record.artifact_revision_id != self.artifact_revision_id
                or str(record.logical_chunk_id) != reference.logical_chunk_id
                or str(record.chunk_revision_id) != reference.chunk_revision_id
                or record.ordinal != reference.ordinal
                or record.content_hash != reference.content_hash
                or (record.token_count or 0) != reference.token_count
            ):
                raise ValueError("chunking record does not match its manifest reference")
        object.__setattr__(
            self,
            "document_id",
            str(self.chunks[0].document_id) if self.chunks else self.artifact_id,
        )
        object.__setattr__(
            self,
            "document_version_id",
            (str(self.chunks[0].document_version_id) if self.chunks else self.artifact_revision_id),
        )
        object.__setattr__(self, "strategy_version", self.manifest.chunker_version)
        object.__setattr__(self, "warnings", self.manifest.validation.warnings)
        object.__setattr__(
            self,
            "statistics",
            ChunkingStatistics(
                route_chunk_count=sum(
                    record.chunk_kind == ChunkKind.ROUTE for record in self.chunks
                ),
                evidence_chunk_count=sum(
                    record.chunk_kind
                    in {
                        ChunkKind.EVIDENCE,
                        ChunkKind.CODE,
                        ChunkKind.COMMENT,
                        ChunkKind.EVENT,
                        ChunkKind.JIRA_FIELD,
                    }
                    for record in self.chunks
                ),
                table_chunk_count=sum(
                    record.chunk_kind == ChunkKind.TABLE for record in self.chunks
                ),
                total_token_count=sum(record.token_count for record in self.chunks),
                rejected_chunk_count=0,
            ),
        )
